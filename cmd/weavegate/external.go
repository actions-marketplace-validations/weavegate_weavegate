package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
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
	java, err := exec.LookPath(e.Java)
	if err != nil {
		return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: java executable is unavailable: %w", err))
	}
	jarDigest, err := digestJAR(e.JAR)
	if err != nil {
		return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: jar: %w", err))
	}
	const millisLimit = int64(math.MaxInt64 / int64(time.Millisecond))
	arrive := int64(cfg.Run.ArriveTimeoutMS)
	if arrive < 1 || arrive > (millisLimit-int64(e.StartupTimeoutMS))/60 {
		return composition{}, ci.InputError(fmt.Errorf("resolve external SUT: startup and arrive budgets exceed supported duration"))
	}
	runMS := int64(e.StartupTimeoutMS) + 60*arrive
	opts := external.Options{
		Java: java, JAR: e.JAR, Commands: commands,
		Points: append([]string(nil), selected.SyncPoints...), Capacity: e.Capacity,
		StartupTimeout: time.Duration(e.StartupTimeoutMS) * time.Millisecond,
		CancelTimeout:  time.Duration(e.CancelTimeoutMS) * time.Millisecond,
		StopTimeout:    time.Duration(e.StopTimeoutMS) * time.Millisecond,
	}
	return composition{
		NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
			current, err := digestJAR(e.JAR)
			if err != nil || current != jarDigest {
				return nil, fmt.Errorf("external SUT jar changed after preflight")
			}
			return external.New(opts, client)
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
	defer file.Close()
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
		content, readErr := io.ReadAll(io.LimitReader(stream, 64*1024))
		_ = stream.Close()
		if readErr != nil {
			return "", fmt.Errorf("%q has an unreadable manifest", path)
		}
		for _, line := range strings.Split(string(content), "\n") {
			if strings.HasPrefix(line, "Main-Class: ") && strings.TrimSpace(strings.TrimPrefix(line, "Main-Class: ")) != "" {
				hasMain = true
			}
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
