package fixture

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/weavegate/weavegate/internal/timing"
)

type statementExecutor interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
}

type sqlSource struct {
	phase string
	name  string
	path  string
}

type preparedSQLSource struct {
	phase      string
	name       string
	contents   []byte
	statements []string
}

type sqlScanState uint8

const (
	sqlStateNormal sqlScanState = iota
	sqlStateSingleQuoted
	sqlStateDoubleQuoted
	sqlStateBacktickQuoted
	sqlStateLineComment
	sqlStateBlockComment
)

// Prepare reads and parses all fixture sources exactly once. Failures here are
// input-shaped and happen before a container is started.
func Prepare(spec FixtureSpec) (Prepared, error) {
	if strings.TrimSpace(spec.Image) == "" {
		return Prepared{}, fmt.Errorf("prepare fixture: image is required")
	}
	migrations, err := migrationSources(spec.Migrations)
	if err != nil {
		return Prepared{}, fmt.Errorf("prepare fixture: %w", err)
	}
	seed, err := seedSource(spec.Seed)
	if err != nil {
		return Prepared{}, fmt.Errorf("prepare fixture: %w", err)
	}

	prepared := Prepared{image: spec.Image, valid: true}
	for _, source := range migrations {
		item, err := prepareSQLSource(source)
		if err != nil {
			return Prepared{}, fmt.Errorf("prepare fixture: %w", err)
		}
		prepared.migrations = append(prepared.migrations, item)
	}
	prepared.migrationDigest = hashMigrations(prepared.migrations)

	prepared.seed, err = prepareSQLSource(seed)
	if err != nil {
		return Prepared{}, fmt.Errorf("prepare fixture: %w", err)
	}
	seedSum := sha256.Sum256(prepared.seed.contents)
	prepared.seedDigest = "sha256:" + hex.EncodeToString(seedSum[:])
	return prepared.clone(), nil
}

func hashMigrations(migrations []preparedSQLSource) string {
	hasher := sha256.New()
	for _, migration := range migrations {
		_, _ = fmt.Fprintf(hasher, "%d\n", len(migration.name))
		_, _ = hasher.Write([]byte(migration.name))
		_, _ = fmt.Fprintf(hasher, "%d\n", len(migration.contents))
		_, _ = hasher.Write(migration.contents)
	}
	return "sha256:" + hex.EncodeToString(hasher.Sum(nil))
}

// applyFixtureSQL applies prepared migrations in lexical filename order,
// followed by the prepared seed statements, without reading source paths.
func applyFixtureSQL(
	ctx context.Context,
	executor statementExecutor,
	prepared Prepared,
) error {
	if executor == nil {
		return fmt.Errorf("apply fixture SQL: executor is required")
	}
	if !prepared.valid {
		return fmt.Errorf("apply fixture SQL: prepared fixture is required")
	}

	sources := append([]preparedSQLSource(nil), prepared.migrations...)
	sources = append(sources, prepared.seed)
	for _, source := range sources {
		sourceCtx, finishSource := timing.Start(ctx, "database_"+source.phase)
		err := executeSQLSource(sourceCtx, executor, source)
		finishSource()
		if err != nil {
			return err
		}
	}

	return nil
}

func migrationSources(directory string) ([]sqlSource, error) {
	if strings.TrimSpace(directory) == "" {
		return nil, fmt.Errorf("load migrations: directory is required")
	}

	entries, err := os.ReadDir(directory)
	if err != nil {
		return nil, fmt.Errorf("read migration directory %q: %w", directory, err)
	}

	sort.Slice(entries, func(i, j int) bool {
		return entries[i].Name() < entries[j].Name()
	})

	var sources []sqlSource
	for _, entry := range entries {
		if filepath.Ext(entry.Name()) != ".sql" {
			continue
		}

		info, err := entry.Info()
		if err != nil {
			return nil, fmt.Errorf("inspect migration file %q: %w", entry.Name(), err)
		}
		if !info.Mode().IsRegular() {
			continue
		}

		sources = append(sources, sqlSource{
			phase: "migration",
			name:  entry.Name(),
			path:  filepath.Join(directory, entry.Name()),
		})
	}

	return sources, nil
}

func seedSource(path string) (sqlSource, error) {
	if strings.TrimSpace(path) == "" {
		return sqlSource{}, fmt.Errorf("load seed: file is required")
	}

	info, err := os.Stat(path)
	if err != nil {
		return sqlSource{}, fmt.Errorf("inspect seed file %q: %w", filepath.Base(path), err)
	}
	if !info.Mode().IsRegular() {
		return sqlSource{}, fmt.Errorf("inspect seed file %q: not a regular file", filepath.Base(path))
	}

	return sqlSource{phase: "seed", name: filepath.Base(path), path: path}, nil
}

func prepareSQLSource(source sqlSource) (preparedSQLSource, error) {
	contents, err := os.ReadFile(source.path)
	if err != nil {
		return preparedSQLSource{}, fmt.Errorf("read %s file %q: %w", source.phase, source.name, err)
	}
	statements, err := splitSQLStatements(string(contents))
	if err != nil {
		return preparedSQLSource{}, fmt.Errorf("parse %s file %q: %w", source.phase, source.name, err)
	}
	return preparedSQLSource{
		phase: source.phase, name: source.name,
		contents:   append([]byte(nil), contents...),
		statements: append([]string(nil), statements...),
	}, nil
}

func executeSQLSource(
	ctx context.Context,
	executor statementExecutor,
	source preparedSQLSource,
) error {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("execute %s file %q: %w", source.phase, source.name, err)
	}

	for statementIndex, statement := range source.statements {
		if _, err := executor.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf(
				"execute %s file %q statement %d: %w",
				source.phase,
				source.name,
				statementIndex+1,
				err,
			)
		}
	}

	return nil
}

func (p Prepared) clone() Prepared {
	cloned := p
	cloned.migrations = make([]preparedSQLSource, len(p.migrations))
	for index, source := range p.migrations {
		cloned.migrations[index] = source.clone()
	}
	cloned.seed = p.seed.clone()
	return cloned
}

func (s preparedSQLSource) clone() preparedSQLSource {
	cloned := s
	cloned.contents = append([]byte(nil), s.contents...)
	cloned.statements = append([]string(nil), s.statements...)
	return cloned
}

// splitSQLStatements handles ordinary MySQL DDL and DML. The fixture contract
// intentionally excludes DELIMITER directives and stored-program bodies.
func splitSQLStatements(source string) ([]string, error) {
	var statements []string
	var statement strings.Builder
	state := sqlStateNormal
	hasExecutableToken := false

	appendStatement := func() {
		trimmed := strings.TrimSpace(statement.String())
		if hasExecutableToken && trimmed != "" {
			statements = append(statements, trimmed)
		}
		statement.Reset()
		hasExecutableToken = false
	}

	for i := 0; i < len(source); i++ {
		current := source[i]
		if state == sqlStateNormal && current == ';' {
			appendStatement()
			continue
		}
		statement.WriteByte(current)

		switch state {
		case sqlStateNormal:
			switch current {
			case '\'':
				hasExecutableToken = true
				state = sqlStateSingleQuoted
			case '"':
				hasExecutableToken = true
				state = sqlStateDoubleQuoted
			case '`':
				hasExecutableToken = true
				state = sqlStateBacktickQuoted
			case '#':
				state = sqlStateLineComment
			case '-':
				if startsDashComment(source, i) {
					statement.WriteByte(source[i+1])
					i++
					state = sqlStateLineComment
				} else {
					hasExecutableToken = true
				}
			case '/':
				if i+1 < len(source) && source[i+1] == '*' {
					statement.WriteByte(source[i+1])
					i++
					if i+1 < len(source) && source[i+1] == '!' {
						hasExecutableToken = true
					}
					state = sqlStateBlockComment
				} else {
					hasExecutableToken = true
				}
			default:
				if current > ' ' {
					hasExecutableToken = true
				}
			}

		case sqlStateSingleQuoted:
			i = consumeStringByte(source, i, '\'', &statement, &state)
		case sqlStateDoubleQuoted:
			i = consumeStringByte(source, i, '"', &statement, &state)
		case sqlStateBacktickQuoted:
			i = consumeBacktickIdentifierByte(source, i, &statement, &state)
		case sqlStateLineComment:
			if current == '\n' || current == '\r' {
				state = sqlStateNormal
			}
		case sqlStateBlockComment:
			if current == '*' && i+1 < len(source) && source[i+1] == '/' {
				statement.WriteByte(source[i+1])
				i++
				state = sqlStateNormal
			}
		}
	}

	switch state {
	case sqlStateSingleQuoted:
		return nil, fmt.Errorf("unterminated single-quoted string")
	case sqlStateDoubleQuoted:
		return nil, fmt.Errorf("unterminated double-quoted string")
	case sqlStateBacktickQuoted:
		return nil, fmt.Errorf("unterminated backtick-quoted identifier")
	case sqlStateBlockComment:
		return nil, fmt.Errorf("unterminated block comment")
	}

	appendStatement()
	return statements, nil
}

func startsDashComment(source string, dashIndex int) bool {
	if dashIndex+1 >= len(source) || source[dashIndex+1] != '-' {
		return false
	}
	if dashIndex+2 >= len(source) {
		return true
	}

	return source[dashIndex+2] <= ' '
}

func consumeStringByte(
	source string,
	index int,
	quote byte,
	statement *strings.Builder,
	state *sqlScanState,
) int {
	current := source[index]
	if current == '\\' && index+1 < len(source) {
		statement.WriteByte(source[index+1])
		return index + 1
	}
	if current != quote {
		return index
	}
	if index+1 < len(source) && source[index+1] == quote {
		statement.WriteByte(source[index+1])
		return index + 1
	}

	*state = sqlStateNormal
	return index
}

func consumeBacktickIdentifierByte(
	source string,
	index int,
	statement *strings.Builder,
	state *sqlScanState,
) int {
	if source[index] != '`' {
		return index
	}
	if index+1 < len(source) && source[index+1] == '`' {
		statement.WriteByte(source[index+1])
		return index + 1
	}

	*state = sqlStateNormal
	return index
}
