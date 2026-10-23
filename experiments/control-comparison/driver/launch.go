package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/weavegate/weavegate/internal/config"
	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/timing"
)

func launch(ctx context.Context, o options, cfg config.Config, db *fixture.DB, x *execution, evaluator oracle.Evaluator, origin time.Time) (returnErr error) {
	before, err := connections(ctx, db)
	if err != nil {
		return err
	}
	d := db.Connection
	password, err := d.Password()
	if err != nil {
		return err
	}
	jdbc := "jdbc:mysql://" + net.JoinHostPort(d.Host, strconv.Itoa(d.Port)) + "/" + url.PathEscape(d.Name)
	cmd := exec.Command(cfg.Target.SUT.External.Java, "-cp", o.classpath, "io.github.weavegate.experiment.BaselineMain", o.application)
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "LANG=C.UTF-8"}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		return err
	}
	_, endStart := timing.Start(ctx, "process_start_to_ready")
	if err = cmd.Start(); err != nil {
		endStart()
		return err
	}
	events := make(chan map[string]any, 4)
	go func() {
		defer close(events)
		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			var event map[string]any
			if json.Unmarshal(scanner.Bytes(), &event) != nil {
				return
			}
			events <- event
		}
	}()
	stopped := false
	stopDeadline := time.Time{}
	endStop := func() {}
	defer func() {
		_ = stdin.Close()
		if !stopped {
			_ = cmd.Process.Signal(syscall.SIGTERM)
		}
		if stopDeadline.IsZero() {
			_, endStop = timing.Start(context.WithoutCancel(ctx), "adapter_stop")
			stopDeadline = time.Now().Add(5 * time.Second)
		}
		if ctx.Err() != nil && time.Until(stopDeadline) > 5*time.Second {
			stopDeadline = time.Now().Add(5 * time.Second)
		}
		x.ChildReaped, err = reap(cmd, stopDeadline)
		endStop()
		// TERM is expected when the collector's budget cancels an otherwise
		// healthy child. A forced KILL/failed deadline remains an error.
		err = cancellationExit(ctx, err)
		returnErr = errors.Join(returnErr, err)
		checkctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		after, checkErr := connections(checkctx, db)
		x.ConnectionsClosed = checkErr == nil && reflectStrings(before, after)
		if !x.ConnectionsClosed {
			returnErr = errors.Join(returnErr, errors.New("application connections remain after child exit"), checkErr)
		}
		if !x.ChildReaped {
			// Wait still owns stderr; do not race its writer or claim reusable state.
			returnErr = errors.Join(returnErr, errors.New("child reap unproven; stderr unavailable"))
			return
		}
		logs := strings.NewReplacer(password, "<redacted>", jdbc, "<redacted-jdbc>").Replace(stderr.String())
		returnErr = errors.Join(returnErr, os.WriteFile(filepath.Join(o.out, fmt.Sprintf("java-%03d.stderr", x.Index)), []byte(logs), 0o600))
	}()
	values := map[string]string{"url": jdbc, "username": d.Username, "password": password, "variant": o.variant}
	for i, w := range cfg.Scenarios[o.scenario].Workers {
		values[fmt.Sprintf("worker.%d", i)] = w.ID
		values[fmt.Sprintf("command.%d", i)] = w.Command
		for k, v := range w.Args {
			values["arg."+k] = v
		}
	}
	for k, v := range values {
		if _, err = fmt.Fprintf(stdin, "%s=%s\n", k, base64.StdEncoding.EncodeToString([]byte(v))); err != nil {
			endStart()
			return err
		}
	}
	if _, err = io.WriteString(stdin, "\n"); err != nil {
		endStart()
		return err
	}
	x.Ready, err = event(ctx, events, "ready", 30*time.Second)
	endStart()
	if err != nil {
		return err
	}
	_, endWork := timing.Start(ctx, "schedule_execute")
	if _, err = io.WriteString(stdin, "GO\n"); err == nil {
		x.Done, err = event(ctx, events, "done", 30*time.Second)
	}
	endWork()
	if err != nil {
		return err
	}
	if x.Ready["hooks_active"] != false || x.Ready["proxies"] != float64(2) || x.Done["active_connections"] != float64(0) || x.Done["workers"] != float64(2) {
		return errors.New("invalid launcher lifecycle evidence")
	}
	x.State, err = snapshot(ctx, db)
	if err != nil {
		return err
	}
	oraclectx, endOracle := timing.Start(ctx, "oracle_evaluate")
	evaluation, err := evaluator.Evaluate(oraclectx, db.SQL, oracle.RunContext{})
	endOracle()
	x.OracleEndNS = time.Since(origin).Nanoseconds()
	if err != nil {
		return err
	}
	x.Evaluation = &evaluation
	_, endStop = timing.Start(context.WithoutCancel(ctx), "adapter_stop")
	stopDeadline = time.Now().Add(20 * time.Second)
	if _, err = io.WriteString(stdin, "STOP\n"); err != nil {
		return err
	}
	_, err = event(ctx, events, "closed", time.Until(stopDeadline))
	stopped = err == nil
	return err
}

func cancellationExit(ctx context.Context, err error) error {
	// Only a direct exit status can be an expected cancellation. A joined reap
	// deadline or cleanup failure must survive even if it contains TERM/143.
	if exit, ok := err.(*exec.ExitError); ctx.Err() != nil && ok {
		if status, ok := exit.Sys().(syscall.WaitStatus); ok {
			// HotSpot's SIGTERM handler exits normally with 128 + SIGTERM.
			if (status.Signaled() && status.Signal() == syscall.SIGTERM) ||
				(status.Exited() && status.ExitStatus() == 128+int(syscall.SIGTERM)) {
				return nil
			}
		}
	}
	return err
}

func event(ctx context.Context, events <-chan map[string]any, name string, limit time.Duration) (map[string]any, error) {
	timer := time.NewTimer(limit)
	defer timer.Stop()
	select {
	case value, ok := <-events:
		if !ok || value["event"] != name {
			return nil, fmt.Errorf("missing launcher event %s", name)
		}
		return value, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-timer.C:
		return nil, fmt.Errorf("launcher %s deadline exceeded", name)
	}
}

// These observations validate lifecycle/seed boundaries, never domain verdicts.
func connections(ctx context.Context, db *fixture.DB) ([]string, error) {
	rows, err := db.SQL.QueryContext(ctx, "SELECT ID FROM information_schema.PROCESSLIST WHERE USER = SUBSTRING_INDEX(CURRENT_USER(), '@', 1) ORDER BY ID")
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	ids := []string{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, errors.Join(rows.Err(), rows.Close())
}

func reflectStrings(a, b []string) bool { return strings.Join(a, ",") == strings.Join(b, ",") }

func snapshot(ctx context.Context, db *fixture.DB) (map[string][]map[string]any, error) {
	rows, err := db.SQL.QueryContext(ctx, "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME")
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	var tables []string
	for rows.Next() {
		var name string
		if err = rows.Scan(&name); err != nil {
			return nil, err
		}
		tables = append(tables, name)
	}
	err = errors.Join(rows.Err(), rows.Close())
	if err != nil {
		return nil, err
	}
	result := map[string][]map[string]any{}
	for _, table := range tables {
		items, err := snapshotTable(ctx, db, table)
		if err != nil {
			return nil, err
		}
		result[table] = items
	}
	return result, nil
}

func snapshotTable(ctx context.Context, db *fixture.DB, table string) ([]map[string]any, error) {
	rows, err := db.SQL.QueryContext(ctx, "SELECT * FROM `"+strings.ReplaceAll(table, "`", "``")+"`")
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	columns, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	items := []map[string]any{}
	for rows.Next() {
		values := make([]any, len(columns))
		pointers := make([]any, len(columns))
		for i := range values {
			pointers[i] = &values[i]
		}
		if err = rows.Scan(pointers...); err != nil {
			return nil, err
		}
		item := map[string]any{}
		for i, k := range columns {
			v := values[i]
			if b, ok := v.([]byte); ok {
				v = string(b)
			}
			item[k] = v
		}
		items = append(items, item)
	}
	if err = errors.Join(rows.Err(), rows.Close()); err != nil {
		return nil, err
	}
	sort.Slice(items, func(i, j int) bool {
		a, _ := json.Marshal(items[i])
		b, _ := json.Marshal(items[j])
		return bytes.Compare(a, b) < 0
	})
	return items, nil
}

// reap shares the stop request's deadline. KILL has one additional bounded reap
// allowance; an uninterruptible process never causes the driver to wait forever.
func reap(cmd *exec.Cmd, deadline time.Time) (bool, error) {
	wait := make(chan error, 1)
	go func() { wait <- cmd.Wait() }()
	timer := time.NewTimer(max(0, time.Until(deadline)))
	defer timer.Stop()
	select {
	case err := <-wait:
		return true, err
	case <-timer.C:
	}
	_ = cmd.Process.Kill()
	killTimer := time.NewTimer(5 * time.Second)
	defer killTimer.Stop()
	select {
	case err := <-wait:
		return true, errors.Join(errors.New("child stop deadline exceeded"), err)
	case <-killTimer.C:
		return false, errors.New("child remains unreaped after KILL deadline")
	}
}
