package external

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

// MaxFrameSize is the wire v1 JSON payload limit, excluding the length header.
const MaxFrameSize = 1 << 20
const maxFrame = MaxFrameSize
const maxSequence = 100000

var errProtocol = errors.New("external SUT protocol violation")
var errTransport = errors.New("external SUT transport failure")
var errVersion = errors.New("external SUT unsupported wire version")

// No decoder error contains input bytes: start frames and untrusted peer
// diagnostics may contain credentials, SQL, or other private application data.
type frame struct {
	V       int            `json:"v"`
	Type    string         `json:"type"`
	Run     string         `json:"run"`
	Session string         `json:"session"`
	Seq     int            `json:"seq"`
	Body    map[string]any `json:"body"`
}

var identityPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)
var arrivalPattern = regexp.MustCompile(`^[1-9][0-9]{0,5}$`)
var sqlStatePattern = regexp.MustCompile(`^[A-Z0-9]{5}$`)

var bodyFields = map[string]string{
	"start":    "variant params commands points capacity database startup_ms cancel_ms",
	"ready":    "commands points capacity",
	"invoke":   "invocation worker command",
	"accepted": "invocation worker",
	"arrive":   "invocation worker arrival point",
	"release":  "invocation worker arrival point",
	"terminal": "invocation worker transaction connection error",
	"cancel":   "invocation worker reason",
	"stop":     "budget_ms",
	"stopped":  "",
	"fatal":    "kind message",
}

func readFrame(r io.Reader) (frame, []byte, error) {
	var header [4]byte
	n, err := io.ReadFull(r, header[:])
	if err != nil {
		if n == 0 && errors.Is(err, io.EOF) {
			return frame{}, nil, io.EOF
		}
		return frame{}, nil, errTransport
	}
	size := binary.BigEndian.Uint32(header[:])
	if size == 0 || size > maxFrame {
		return frame{}, nil, errProtocol
	}
	raw := make([]byte, size)
	if _, err := io.ReadFull(r, raw); err != nil {
		return frame{}, nil, errTransport
	}
	f, err := decodeFrame(raw)
	return f, raw, err
}

func writeFrame(w io.Writer, f frame) error {
	raw, err := json.Marshal(f)
	if err != nil || len(raw) == 0 || len(raw) > maxFrame {
		return errProtocol
	}
	buf := make([]byte, 4+len(raw))
	binary.BigEndian.PutUint32(buf, uint32(len(raw)))
	copy(buf[4:], raw)
	for len(buf) > 0 {
		n, err := w.Write(buf)
		if err != nil || n <= 0 {
			return errTransport
		}
		buf = buf[n:]
	}
	return nil
}

// JSON marshaling replaces invalid Go UTF-8 with U+FFFD. Validate strings in
// our outbound body types before marshaling, so decoding cannot hide a change
// to configuration or credentials. Schema validation still follows encoding.
func validOutboundUTF8(v any) bool {
	switch v := v.(type) {
	case string:
		return utf8.ValidString(v)
	case map[string]any:
		for key, value := range v {
			if !utf8.ValidString(key) || !validOutboundUTF8(value) {
				return false
			}
		}
	case map[string]string:
		for key, value := range v {
			if !utf8.ValidString(key) || !utf8.ValidString(value) {
				return false
			}
		}
	case []string:
		for _, value := range v {
			if !utf8.ValidString(value) {
				return false
			}
		}
	}
	return true
}

func decodeFrame(raw []byte) (frame, error) {
	if !utf8.Valid(raw) || !validEscapes(raw) {
		return frame{}, errProtocol
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	v, err := decodeValue(d, 0)
	if err != nil {
		return frame{}, errProtocol
	}
	if _, err := d.Token(); err != io.EOF {
		return frame{}, errProtocol
	}
	m, ok := v.(map[string]any)
	if !ok || !fields(m, "v type run session seq body") {
		return frame{}, errProtocol
	}
	if !integer(m["v"], 1, 2147483647) || !integer(m["seq"], 1, maxSequence) {
		return frame{}, errProtocol
	}
	b, ok := m["body"].(map[string]any)
	if !ok || !identity(m["run"]) || !identity(m["session"]) {
		return frame{}, errProtocol
	}
	f := frame{V: number(m["v"]), Type: str(m["type"]), Run: str(m["run"]), Session: str(m["session"]), Seq: number(m["seq"]), Body: b}
	if !validBody(f.Type, b) {
		return frame{}, errProtocol
	}
	if f.V != 1 {
		return frame{}, errVersion
	}
	return f, nil
}

// Token decoding rejects duplicate keys at every nesting level. Limit nesting
// independently of frame size to avoid exhausting the stack on malformed input.
func decodeValue(d *json.Decoder, depth int) (any, error) {
	if depth > 32 {
		return nil, errProtocol
	}
	t, err := d.Token()
	if err != nil {
		return nil, err
	}
	switch t {
	case json.Delim('{'):
		m := map[string]any{}
		for d.More() {
			k, err := d.Token()
			if err != nil {
				return nil, err
			}
			key, ok := k.(string)
			if !ok {
				return nil, errProtocol
			}
			if _, exists := m[key]; exists {
				return nil, errProtocol
			}
			v, err := decodeValue(d, depth+1)
			if err != nil {
				return nil, err
			}
			m[key] = v
		}
		end, err := d.Token()
		if err != nil || end != json.Delim('}') {
			return nil, errProtocol
		}
		return m, nil
	case json.Delim('['):
		a := []any{}
		for d.More() {
			v, err := decodeValue(d, depth+1)
			if err != nil {
				return nil, err
			}
			a = append(a, v)
		}
		end, err := d.Token()
		if err != nil || end != json.Delim(']') {
			return nil, errProtocol
		}
		return a, nil
	default:
		if _, ok := t.(json.Delim); ok {
			return nil, errProtocol
		}
		return t, nil
	}
}

// encoding/json replaces unpaired UTF-16 escapes with U+FFFD. Reject those
// escapes before decoding, while preserving legitimate literal U+FFFD strings.
func validEscapes(raw []byte) bool {
	for i := 0; i < len(raw); i++ {
		if raw[i] != '\\' {
			continue
		}
		i++
		if i >= len(raw) {
			return false
		}
		if raw[i] != 'u' {
			continue
		}
		if i+4 >= len(raw) {
			return false
		}
		n, err := strconv.ParseUint(string(raw[i+1:i+5]), 16, 16)
		if err != nil {
			return false
		}
		i += 4
		if n >= 0xdc00 && n <= 0xdfff {
			return false
		}
		if n < 0xd800 || n > 0xdbff {
			continue
		}
		if i+6 >= len(raw) || string(raw[i+1:i+3]) != `\u` {
			return false
		}
		n, err = strconv.ParseUint(string(raw[i+3:i+7]), 16, 16)
		if err != nil || n < 0xdc00 || n > 0xdfff {
			return false
		}
		i += 6
	}
	return true
}

func fields(m map[string]any, names string) bool {
	want := strings.Fields(names)
	if len(m) != len(want) {
		return false
	}
	for _, k := range want {
		if _, ok := m[k]; !ok {
			return false
		}
	}
	return true
}
func str(v any) string { s, _ := v.(string); return s }
func number(v any) int { n, _ := strconv.Atoi(string(v.(json.Number))); return n }
func integer(v any, low, high int) bool {
	n, ok := v.(json.Number)
	if !ok {
		return false
	}
	i, err := strconv.ParseInt(string(n), 10, 64)
	return err == nil && i >= int64(low) && i <= int64(high)
}
func identity(v any) bool { return identityPattern.MatchString(str(v)) }
func name(v any) bool {
	s, ok := v.(string)
	if !ok || s == "" || len(s) > 128 || strings.TrimSpace(s) != s || !utf8.ValidString(s) {
		return false
	}
	return !strings.ContainsFunc(s, unicode.IsControl)
}
func textField(v any, nonempty bool, max int) bool {
	s, ok := v.(string)
	return ok && utf8.ValidString(s) && (!nonempty || s != "") && (max == 0 || len(s) <= max)
}
func names(v any) bool {
	a, ok := v.([]any)
	if !ok {
		return false
	}
	seen := map[string]bool{}
	for _, v := range a {
		if !name(v) || seen[str(v)] {
			return false
		}
		seen[str(v)] = true
	}
	return true
}

func validBody(kind string, b map[string]any) bool {
	shape, ok := bodyFields[kind]
	if !ok || !fields(b, shape) {
		return false
	}
	if v, ok := b["invocation"]; ok && (!identity(v) || !name(b["worker"])) {
		return false
	}
	if v, ok := b["arrival"]; ok {
		n, err := strconv.Atoi(str(v))
		if !arrivalPattern.MatchString(str(v)) || err != nil || n > maxSequence || !name(b["point"]) {
			return false
		}
	}
	for _, k := range []string{"startup_ms", "cancel_ms", "budget_ms"} {
		if v, ok := b[k]; ok && !integer(v, 1, 2147483647) {
			return false
		}
	}
	if kind == "start" || kind == "ready" {
		if !integer(b["capacity"], 1, 1024) || !names(b["commands"]) || !names(b["points"]) {
			return false
		}
	}
	switch kind {
	case "start":
		db, ok := b["database"].(map[string]any)
		if !ok || !fields(db, "driver host port name username password") || db["driver"] != "mysql" || !integer(db["port"], 1, 65535) || !name(b["variant"]) {
			return false
		}
		for _, k := range []string{"host", "name", "username", "password"} {
			if !textField(db[k], k != "password", 0) {
				return false
			}
		}
		params, ok := b["params"].(map[string]any)
		if !ok {
			return false
		}
		for k, v := range params {
			if !name(k) || !textField(v, false, 0) {
				return false
			}
		}
	case "invoke":
		return name(b["command"])
	case "cancel":
		return b["reason"] == "context" || b["reason"] == "stop"
	case "terminal":
		tx, conn := b["transaction"], b["connection"]
		if tx != "committed" && tx != "rolled_back" && tx != "not_started" {
			return false
		}
		if conn != "returned" && (conn != "not_acquired" || tx != "not_started") {
			return false
		}
		if b["error"] == nil {
			return tx == "committed" && conn == "returned"
		}
		e, ok := b["error"].(map[string]any)
		if !ok || !fields(e, "kind message mysql_code sql_state") || !textField(e["message"], false, 1024) || !integer(e["mysql_code"], 0, 65535) {
			return false
		}
		if e["kind"] == "mysql" {
			return number(e["mysql_code"]) > 0 && sqlStatePattern.MatchString(str(e["sql_state"]))
		}
		return (e["kind"] == "application" || e["kind"] == "cancelled") && number(e["mysql_code"]) == 0 && e["sql_state"] == ""
	case "fatal":
		return slices.Contains([]string{"version", "protocol", "startup", "transport", "transaction", "cleanup", "shutdown"}, str(b["kind"])) && textField(b["message"], false, 1024)
	}
	return true
}
