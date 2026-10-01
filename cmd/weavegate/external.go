package main

import (
	"archive/zip"
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
		NewAdapter: func(ctx context.Context, client syncpoint.Client) (sut.Adapter, error) {
			snapshot, err := snapshotJAR(ctx, e.JAR, jarDigest)
			if err != nil {
				return nil, err
			}
			launchOpts := opts
			launchOpts.JAR = snapshot
			adapter, err := external.New(launchOpts, client)
			if err != nil {
				_ = os.RemoveAll(filepath.Dir(snapshot))
				return nil, err
			}
			return &snapshotAdapter{Adapter: adapter, path: snapshot}, nil
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

func snapshotJAR(ctx context.Context, path, expected string) (_ string, err error) {
	if err := ctx.Err(); err != nil {
		return "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	source, err := os.Open(path)
	if err != nil {
		return "", fmt.Errorf("external SUT jar changed after preflight: %w", err)
	}
	defer func() { _ = source.Close() }()
	dir, err := os.MkdirTemp("", "weavegate-jar-")
	if err != nil {
		return "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	defer func() {
		if err != nil {
			_ = os.RemoveAll(dir)
		}
	}()
	pathSnapshot := filepath.Join(dir, "sut.jar")
	target, err := os.OpenFile(pathSnapshot, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	h := sha256.New()
	_, copyErr := copyWithContext(ctx, io.MultiWriter(target, h), source)
	closeErr := target.Close()
	if copyErr != nil || closeErr != nil {
		return "", fmt.Errorf("snapshot external SUT jar: %w", errors.Join(copyErr, closeErr))
	}
	if hex.EncodeToString(h.Sum(nil)) != expected {
		return "", fmt.Errorf("external SUT jar changed after preflight")
	}
	if err := ctx.Err(); err != nil {
		return "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	if err := os.Chmod(pathSnapshot, 0o400); err != nil {
		return "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return "", fmt.Errorf("snapshot external SUT jar: %w", err)
	}
	return pathSnapshot, nil
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
	sut.Adapter
	path string
}

func (a *snapshotAdapter) Stop(ctx context.Context) error {
	return errors.Join(a.Adapter.Stop(ctx), os.RemoveAll(filepath.Dir(a.path)))
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
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer func() { _ = file.Close() }()
	info, err := file.Stat()
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() || filepath.Ext(path) != ".jar" {
		return "", fmt.Errorf("%q must name a regular .jar file", path)
	}
	archive, err := zip.NewReader(file, info.Size())
	if err != nil {
		return "", fmt.Errorf("%q is not a readable jar", path)
	}
	hasMain := false
	for _, entry := range archive.File {
		if entry.Name != "META-INF/MANIFEST.MF" {
			continue
		}
		stream, err := entry.Open()
		if err != nil {
			return "", fmt.Errorf("%q has an unreadable manifest", path)
		}
		content, readErr := io.ReadAll(io.LimitReader(stream, 64*1024+1))
		_ = stream.Close()
		if readErr != nil || len(content) > 64*1024 {
			return "", fmt.Errorf("%q has an unreadable manifest", path)
		}
		var hasClassPath bool
		hasMain, hasClassPath = mainManifestAttributes(content)
		if hasClassPath {
			return "", fmt.Errorf("%q has a Class-Path manifest entry; external SUT jars must be self-contained", path)
		}
		break
	}
	if !hasMain {
		return "", fmt.Errorf("%q has no Main-Class manifest entry", path)
	}
	h := sha256.New()
	if _, err := io.Copy(h, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// Java reads launch attributes from the first manifest section only. A blank
// line starts named entry sections, whose attributes do not affect java -jar.
func mainManifestAttributes(content []byte) (hasMain, hasClassPath bool) {
	var key, value string
	flush := func() {
		switch {
		case strings.EqualFold(key, "Main-Class") && strings.TrimSpace(value) != "":
			hasMain = true
		case strings.EqualFold(key, "Class-Path") && strings.TrimSpace(value) != "":
			hasClassPath = true
		}
	}
	for _, raw := range strings.Split(string(content), "\n") {
		line := strings.TrimSuffix(raw, "\r")
		if line == "" {
			flush()
			break
		}
		if strings.HasPrefix(line, " ") {
			value += strings.TrimPrefix(line, " ")
			continue
		}
		flush()
		key, value, _ = strings.Cut(line, ": ")
	}
	flush()
	return hasMain, hasClassPath
}
