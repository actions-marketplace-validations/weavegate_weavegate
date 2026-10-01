package config

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// rawConfig mirrors Config for decoding. run's three fields use pointers so
// an omitted key can be told apart from an explicit zero: applying a
// default to a *nil* field is correct, but applying it to an explicit 0
// would silently swallow the value Config.Validate is supposed to reject.
type rawConfig struct {
	Target    Target              `yaml:"target"`
	Scenarios map[string]Scenario `yaml:"scenarios"`
	Oracle    rawOracle           `yaml:"oracle"`
	Run       rawRun              `yaml:"run"`
}

type rawRun struct {
	Repeat          *int `yaml:"repeat"`
	ArriveTimeoutMS *int `yaml:"arrive_timeout_ms"`
	ExplorePasses   *int `yaml:"explore_passes"`
}

type rawOracle struct {
	Assertions []rawAssertion `yaml:"assertions"`
}

// rawAssertion's ExpectRows is a pointer for the same reason rawRun's
// fields are: expect_rows has no valid default (every assertion must
// declare it), so an omitted key has to surface as its own error rather
// than decoding to the same 0 as an explicit "expect_rows: 0" — the one
// value this oracle actually accepts.
type rawAssertion struct {
	ID         string `yaml:"id"`
	SQL        string `yaml:"sql"`
	ExpectRows *int   `yaml:"expect_rows"`
}

// toConfig applies defaults only to fields that were actually omitted
// (nil); an explicit value — including an explicit zero — is carried
// through verbatim so Config.Validate's positive-value constraint still
// gets a chance to reject it, instead of a default silently standing in
// for it before validation ever sees the value the user wrote.
func (raw rawConfig) toConfig() (Config, error) {
	cfg := Config{
		Target:    raw.Target,
		Scenarios: raw.Scenarios,
	}

	cfg.Run.Repeat = DefaultRepeat
	if raw.Run.Repeat != nil {
		cfg.Run.Repeat = *raw.Run.Repeat
	}
	cfg.Run.ArriveTimeoutMS = DefaultArriveTimeoutMS
	if raw.Run.ArriveTimeoutMS != nil {
		cfg.Run.ArriveTimeoutMS = *raw.Run.ArriveTimeoutMS
	}
	cfg.Run.ExplorePasses = DefaultExplorePasses
	if raw.Run.ExplorePasses != nil {
		cfg.Run.ExplorePasses = *raw.Run.ExplorePasses
	}

	cfg.Oracle.Assertions = make([]Assertion, 0, len(raw.Oracle.Assertions))
	for index, assertion := range raw.Oracle.Assertions {
		if assertion.ExpectRows == nil {
			return Config{}, fmt.Errorf("oracle.assertions[%d] %q: expect_rows is required", index, assertion.ID)
		}
		cfg.Oracle.Assertions = append(cfg.Oracle.Assertions, Assertion{
			ID:         assertion.ID,
			SQL:        assertion.SQL,
			ExpectRows: *assertion.ExpectRows,
		})
	}

	return cfg, nil
}

// Load decodes and validates the run configuration at path. Relative schema
// paths are resolved against the directory containing path.
func Load(path string) (Config, error) {
	file, err := os.Open(path)
	if err != nil {
		return Config{}, fmt.Errorf("load config %q: %w", path, err)
	}
	defer func() { _ = file.Close() }()

	decoder := yaml.NewDecoder(file)
	decoder.KnownFields(true)

	var raw rawConfig
	if err := decoder.Decode(&raw); err != nil {
		return Config{}, fmt.Errorf("load config %q: %w (see docs/reference/config.md)", path, err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			err = fmt.Errorf("multiple YAML documents are not supported")
		}
		return Config{}, fmt.Errorf("load config %q: %w (see docs/reference/config.md)", path, err)
	}

	cfg, err := raw.toConfig()
	if err != nil {
		return Config{}, fmt.Errorf("load config %q: %w", path, err)
	}
	cfg.resolveRelativePaths(filepath.Dir(path))

	if err := cfg.Validate(); err != nil {
		return Config{}, fmt.Errorf("load config %q: %w", path, err)
	}

	return cfg, nil
}

func (c *Config) resolveRelativePaths(baseDir string) {
	c.Target.Schema.Migrations = resolvePath(baseDir, c.Target.Schema.Migrations)
	c.Target.Schema.Seed = resolvePath(baseDir, c.Target.Schema.Seed)
	if c.Target.SUT.External != nil {
		e := c.Target.SUT.External
		e.JAR = resolvePath(baseDir, e.JAR)
		if strings.ContainsRune(e.Java, filepath.Separator) {
			e.Java = resolvePath(baseDir, e.Java)
		}
	}
}

func resolvePath(baseDir, path string) string {
	if path == "" || filepath.IsAbs(path) {
		return path
	}
	return filepath.Clean(filepath.Join(baseDir, path))
}
