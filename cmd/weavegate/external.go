package main

import (
	"archive/zip"
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/weavegate/weavegate/internal/ci"
	"github.com/weavegate/weavegate/internal/config"
	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/sut/external"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

// bindExternal performs all launch and registration checks that do not need a
// provisioned fixture. The selected scenario is the registration vocabulary;
// the Java peer confirms it at ready before any command is admitted.
func bindExternal(cfg config.Config, selected config.Scenario, variant string) (composition, error) {
	e := cfg.Target.SUT.External
	if e == nil {
		return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: target.sut.external is required"))
	}
	if err := e.Validate(); err != nil {
		return composition{}, ci.InputError(err)
	}
	if !wireName(variant) {
		return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: target.sut.variant is not a valid wire name"))
	}
	if e.Capacity < len(selected.Workers) {
		return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: capacity %d is smaller than %d scenario workers", e.Capacity, len(selected.Workers)))
	}
	if len(selected.Workers) == 0 {
		return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: selected scenario has no workers"))
	}
	commands := make([]string, 0, len(selected.Workers))
	for _, worker := range selected.Workers {
		if !wireName(worker.ID) || !wireName(worker.Command) {
			return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: worker %q has an invalid wire id or command", worker.ID))
		}
		if !slices.Contains(commands, worker.Command) {
			commands = append(commands, worker.Command)
		}
		for key, value := range worker.Args {
			if !wireName(key) || !utf8.ValidString(value) {
				return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: worker %q has an invalid parameter", worker.ID))
			}
		}
	}
	slices.Sort(commands)
	for _, point := range selected.SyncPoints {
		if !wireName(point) {
			return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: invalid sync point %q", point))
		}
	}
	if err := validateExternalStartSize(variant, selected.Workers[0].Args, commands, selected.SyncPoints, e.Capacity, e.CancelTimeoutMS); err != nil {
		return composition{}, ci.InputError(err)
	}
	java, err := exec.LookPath(e.Java)
	if err != nil {
		return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: java executable is unavailable: %w", err))
	}
	jarDigest, err := digestJAR(e.JAR)
	if err != nil {
		return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: jar: %w", err))
	}
	var wireRun [16]byte
	if _, err := rand.Read(wireRun[:]); err != nil {
		return composition{}, fmt.Errorf("resolve external SUT: generate wire run ID: %w", err)
	}
	const millisLimit = int64(math.MaxInt64 / int64(time.Millisecond))
	arrive := int64(cfg.Run.ArriveTimeoutMS)
	if arrive < 1 || arrive > (millisLimit-int64(e.StartupTimeoutMS))/60 {
		return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: startup and arrive budgets exceed supported duration"))
	}
	runMS := int64(e.StartupTimeoutMS) + 60*arrive
	opts := external.Options{
		Java: java, Commands: commands, RunID: hex.EncodeToString(wireRun[:]),
		Points: append([]string(nil), selected.SyncPoints...), Capacity: e.Capacity,
		StartupTimeout: time.Duration(e.StartupTimeoutMS) * time.Millisecond,
		CancelTimeout:  time.Duration(e.CancelTimeoutMS) * time.Millisecond,
		StopTimeout:    time.Duration(e.StopTimeoutMS) * time.Millisecond,
	}
	return composition{
		NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
			return &snapshotAdapter{client: client, opts: opts, jar: e.JAR, digest: jarDigest}, nil
		},
		Variants: []string{variant},
		Timeouts: &Timeouts{
			BlockInference: time.Duration(arrive) * time.Millisecond,
			Step:           time.Duration(20*arrive) * time.Millisecond,
			Run:            time.Duration(runMS) * time.Millisecond,
			Stop:           opts.StopTimeout,
		},
		SUTSHA256: "sha256:" + jarDigest,
	}, nil
}

// A filesystem operation may remain blocked even after its file is closed.
// Keep that operation off the run goroutine, and let its owner clean up any
// late snapshot once the filesystem responds.
func boundedSnapshot(ctx context.Context, snapshot func() (string, error)) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	type result struct {
		path string
		err  error
	}
	done := make(chan result)
	go func() {
		path, err := snapshot()
		select {
		case done <- result{path, err}:
		case <-ctx.Done():
			if path != "" {
				_ = os.RemoveAll(filepath.Dir(path))
			}
		}
	}()
	select {
	case result := <-done:
		if err := ctx.Err(); err != nil {
			if result.path != "" {
				_ = os.RemoveAll(filepath.Dir(result.path))
			}
			return "", fmt.Errorf("snapshot external SUT jar: %w", err)
		}
		return result.path, result.err
	case <-ctx.Done():
		return "", fmt.Errorf("snapshot external SUT jar: %w", ctx.Err())
	}
}

// The fixture supplies the database descriptor later. Reserve room for its
// fields while bounding every byte contributed by the selected scenario.
const externalDatabaseReserve = 4 << 10

func validateExternalStartSize(variant string, params map[string]string, commands, points []string, capacity, cancelMS int) error {
	frame := map[string]any{
		"v": 1, "type": "start", "run": strings.Repeat("0", 32),
		"session": strings.Repeat("0", 32), "seq": 1,
		"body": map[string]any{
			"variant": variant, "params": params, "commands": commands,
			"points": points, "capacity": capacity, "startup_ms": 2147483647,
			"cancel_ms": cancelMS, "database": map[string]any{},
		},
	}
	raw, err := json.Marshal(frame)
	if err != nil || len(raw) > external.MaxFrameSize-externalDatabaseReserve {
		return fmt.Errorf("resolve external SUT: selected scenario exceeds the start frame size limit")
	}
	return nil
}

func snapshotJAR(ctx context.Context, path, expected string) (string, error) {
	snapshot, actual, err := copyJARImage(ctx, path)
	if err != nil {
		return "", err
	}
	if actual != expected {
		_ = os.RemoveAll(filepath.Dir(snapshot))
		return "", fmt.Errorf("external SUT jar changed after preflight")
	}
	return snapshot, nil
}

// copyJARImage hashes only bytes written to the private image. Preflight
// validates that image, so the advertised digest and manifest describe the
// same immutable bytes even if the configured path is rewritten in place.
func copyJARImage(ctx context.Context, path string) (_ string, _ string, err error) {
	if err := ctx.Err(); err != nil {
		return "", "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	source, err := os.Open(path)
	if err != nil {
		return "", "", fmt.Errorf("open external SUT jar: %w", err)
	}
	defer func() { _ = source.Close() }()
	dir, err := os.MkdirTemp("", "weavegate-jar-")
	if err != nil {
		return "", "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	defer func() {
		if err != nil {
			_ = os.RemoveAll(dir)
		}
	}()
	pathSnapshot := filepath.Join(dir, "sut.jar")
	target, err := os.OpenFile(pathSnapshot, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return "", "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	copyDone := make(chan struct{})
	defer close(copyDone)
	go func() {
		select {
		case <-ctx.Done():
			_ = source.Close()
			_ = target.Close()
		case <-copyDone:
		}
	}()
	h := sha256.New()
	_, copyErr := copyWithContext(ctx, io.MultiWriter(target, h), source)
	closeErr := target.Close()
	if copyErr != nil || closeErr != nil {
		return "", "", fmt.Errorf("snapshot external SUT jar: %w", errors.Join(copyErr, closeErr))
	}
	if err := ctx.Err(); err != nil {
		return "", "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	if err := os.Chmod(pathSnapshot, 0o400); err != nil {
		return "", "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return "", "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	return pathSnapshot, hex.EncodeToString(h.Sum(nil)), nil
}

func copyWithContext(ctx context.Context, dst io.Writer, src io.Reader) (int64, error) {
	buf := make([]byte, 32*1024)
	var total int64
	for {
		if err := ctx.Err(); err != nil {
			return total, err
		}
		n, readErr := src.Read(buf)
		if n > 0 {
			written, writeErr := dst.Write(buf[:n])
			total += int64(written)
			if writeErr != nil {
				return total, writeErr
			}
			if written != n {
				return total, io.ErrShortWrite
			}
		}
		if readErr == io.EOF {
			return total, ctx.Err()
		}
		if readErr != nil {
			return total, readErr
		}
	}
}

type snapshotAdapter struct {
	client  syncpoint.Client
	opts    external.Options
	jar     string
	digest  string
	adapter sut.Adapter
	path    string
}

func (a *snapshotAdapter) Start(ctx context.Context, cfg sut.SUTConfig, db *fixture.DB) (sut.Handle, error) {
	if a.adapter != nil {
		return nil, fmt.Errorf("external SUT adapter already started")
	}
	snapshot, err := boundedSnapshot(ctx, func() (string, error) {
		return snapshotJAR(ctx, a.jar, a.digest)
	})
	if err != nil {
		return nil, err
	}
	launchOpts := a.opts
	launchOpts.JAR = snapshot
	adapter, err := external.New(launchOpts, a.client)
	if err != nil {
		_ = os.RemoveAll(filepath.Dir(snapshot))
		return nil, err
	}
	a.adapter, a.path = adapter, snapshot
	return adapter.Start(ctx, cfg, db)
}

func (a *snapshotAdapter) Stop(ctx context.Context) error {
	var stopErr, removeErr error
	if a.adapter != nil {
		stopErr = a.adapter.Stop(ctx)
	}
	if a.path != "" {
		removeErr = os.RemoveAll(filepath.Dir(a.path))
	}
	return errors.Join(stopErr, removeErr)
}

func wireName(value string) bool {
	if value == "" || len(value) > 128 || strings.TrimSpace(value) != value || !utf8.ValidString(value) {
		return false
	}
	for _, r := range value {
		if unicode.IsControl(r) {
			return false
		}
	}
	return true
}

func digestJAR(path string) (string, error) {
	info, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() || filepath.Ext(path) != ".jar" {
		return "", fmt.Errorf("%q must name a regular .jar file", path)
	}
	snapshot, digest, err := copyJARImage(context.Background(), path)
	if err != nil {
		return "", err
	}
	defer func() { _ = os.RemoveAll(filepath.Dir(snapshot)) }()
	if err := validateJARImage(snapshot, path); err != nil {
		return "", err
	}
	return digest, nil
}

func validateJARImage(snapshot, displayPath string) error {
	file, err := os.Open(snapshot)
	if err != nil {
		return err
	}
	defer func() { _ = file.Close() }()
	info, err := file.Stat()
	if err != nil {
		return err
	}
	archive, err := zip.NewReader(file, info.Size())
	if err != nil {
		return fmt.Errorf("%q is not a readable jar", displayPath)
	}
	var manifest *zip.File
	for _, entry := range archive.File {
		if entry.Name != "META-INF/MANIFEST.MF" {
			continue
		}
		if manifest != nil {
			return fmt.Errorf("%q has duplicate manifest entries", displayPath)
		}
		manifest = entry
	}
	if manifest == nil {
		return fmt.Errorf("%q has no Main-Class manifest entry", displayPath)
	}
	stream, err := manifest.Open()
	if err != nil {
		return fmt.Errorf("%q has an unreadable manifest", displayPath)
	}
	hasMain, hasClassPath, parseErr := mainManifestAttributes(stream)
	_, drainErr := io.Copy(io.Discard, stream)
	closeErr := stream.Close()
	if err := errors.Join(parseErr, drainErr, closeErr); err != nil {
		return fmt.Errorf("%q has an unreadable manifest: %w", displayPath, err)
	}
	if hasClassPath {
		return fmt.Errorf("%q has a Class-Path manifest entry; external SUT jars must be self-contained", displayPath)
	}
	if !hasMain {
		return fmt.Errorf("%q has no Main-Class manifest entry", displayPath)
	}
	return nil
}

// Java reads launch attributes from the first manifest section only. A blank
// line starts named entry sections, whose attributes do not affect java -jar.
func mainManifestAttributes(stream io.Reader) (hasMain, hasClassPath bool, err error) {
	reader := bufio.NewReader(stream)
	var key string
	var valuePresent bool
	var seenMain, seenClassPath bool
	flush := func() error {
		switch {
		case strings.EqualFold(key, "Main-Class"):
			if seenMain {
				return fmt.Errorf("duplicate Main-Class manifest attribute")
			}
			seenMain, hasMain = true, valuePresent
		case strings.EqualFold(key, "Class-Path"):
			if seenClassPath {
				return fmt.Errorf("duplicate Class-Path manifest attribute")
			}
			seenClassPath, hasClassPath = true, valuePresent
		}
		return nil
	}
	for {
		prefix, tailValue, readErr := readManifestLine(reader)
		if readErr == io.EOF {
			return hasMain, hasClassPath, flush()
		}
		if readErr != nil {
			return false, false, readErr
		}
		line := prefix
		if len(line) == 0 {
			return hasMain, hasClassPath, flush()
		}
		if line[0] == ' ' {
			valuePresent = valuePresent || len(bytes.TrimSpace(line[1:])) > 0 || tailValue
			continue
		}
		if err := flush(); err != nil {
			return false, false, err
		}
		name, value, ok := bytes.Cut(line, []byte(": "))
		if !ok {
			key, valuePresent = "", false
			continue
		}
		key = string(name)
		valuePresent = len(bytes.TrimSpace(value)) > 0 || tailValue
	}
}

// Retain only the beginning of each line: manifest keys are short, while
// values and later named sections can be arbitrarily large. The rest is
// scanned for non-whitespace without retaining it.
func readManifestLine(reader *bufio.Reader) (prefix []byte, tailValue bool, err error) {
	const prefixLimit = 256
	sawByte := false
	for {
		b, readErr := reader.ReadByte()
		if readErr != nil {
			if readErr == io.EOF && sawByte {
				return prefix, tailValue, nil
			}
			return nil, false, readErr
		}
		sawByte = true
		if b == '\n' {
			return prefix, tailValue, nil
		}
		if b == '\r' {
			if next, err := reader.Peek(1); err == nil && next[0] == '\n' {
				_, _ = reader.Discard(1)
			}
			return prefix, tailValue, nil
		}
		if len(prefix) < prefixLimit {
			prefix = append(prefix, b)
		} else if b != ' ' && b != '\t' {
			tailValue = true
		}
	}
}
