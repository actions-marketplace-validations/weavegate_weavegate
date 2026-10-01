package config

import (
	"path/filepath"
	"strings"
	"testing"
)

func externalYAML(t *testing.T) string {
	t.Helper()
	base := loadValidBytes(t)
	base = strings.Replace(base, "    adapter: gonative\n    entrypoint: matching-slice\n    variant: vulnerable\n", "    adapter: external\n    variant: vulnerable\n    external:\n      java: java\n      jar: ../app/seat.jar\n      capacity: 2\n      startup_timeout_ms: 30000\n      cancel_timeout_ms: 5000\n      stop_timeout_ms: 10000\n", 1)
	return base
}

func TestExternalConfigLoad(t *testing.T) {
	base := externalYAML(t)
	path := writeConfig(t, base)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load external: %v", err)
	}
	if cfg.Target.SUT.External == nil || cfg.Target.SUT.External.JAR != filepath.Clean(filepath.Join(filepath.Dir(path), "../app/seat.jar")) {
		t.Fatalf("external path not resolved: %+v", cfg.Target.SUT.External)
	}
	javaPath := strings.Replace(base, "java: java", "java: ./bin/java", 1)
	javaConfigPath := writeConfig(t, javaPath)
	javaCfg, err := Load(javaConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	if javaCfg.Target.SUT.External.Java != filepath.Join(filepath.Dir(javaConfigPath), "bin/java") {
		t.Fatalf("java path = %q", javaCfg.Target.SUT.External.Java)
	}
	cases := map[string]string{
		"missing launch":         strings.Replace(base, "    external:\n", "    absent:\n", 1),
		"entrypoint combination": strings.Replace(base, "    variant: vulnerable\n", "    entrypoint: matching-slice\n    variant: vulnerable\n", 1),
		"missing java":           strings.Replace(base, "      java: java\n", "", 1),
		"missing jar":            strings.Replace(base, "      jar: ../app/seat.jar\n", "", 1),
		"capacity":               strings.Replace(base, "      capacity: 2\n", "      capacity: 0\n", 1),
		"startup budget":         strings.Replace(base, "      startup_timeout_ms: 30000\n", "      startup_timeout_ms: 0\n", 1),
		"cancel exceeds stop":    strings.Replace(base, "      cancel_timeout_ms: 5000\n", "      cancel_timeout_ms: 20000\n", 1),
		"unknown protocol key":   strings.Replace(base, "      capacity: 2\n", "      capacity: 2\n      wire_version: 2\n", 1),
	}
	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := Load(writeConfig(t, content)); err == nil {
				t.Fatal("expected rejection")
			}
		})
	}
	gonative := loadValidBytes(t)
	gonative = strings.Replace(gonative, "    variant: vulnerable\n", "    variant: vulnerable\n    external:\n      java: java\n", 1)
	if _, err := Load(writeConfig(t, gonative)); err == nil {
		t.Fatal("gonative accepted external keys")
	}
}
