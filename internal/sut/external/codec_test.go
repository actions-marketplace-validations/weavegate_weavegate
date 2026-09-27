package external

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"
)

const vectorPath = "../../../docs/reference/testdata/external-sut-v1.json"
const vectorDigest = "a04520c31157d9eedd174a9ed3c12bc54a9676b2e9a7fda6b04fa1c591563450"

type vectorStep struct {
	Prefix   string          `json:"prefix"`
	Peer     string          `json:"peer"`
	Action   string          `json:"action"`
	Event    string          `json:"event"`
	Args     json.RawMessage `json:"args"`
	Frame    json.RawMessage `json:"frame"`
	Expect   []string        `json:"expect"`
	Delivery string          `json:"delivery"`
}
type vectors struct {
	Prefixes map[string][]vectorStep `json:"prefixes"`
	Cases    []struct {
		ID      string       `json:"id"`
		Prefix  string       `json:"prefix"`
		Steps   []vectorStep `json:"steps"`
		Targets []string     `json:"targets"`
	} `json:"cases"`
	Framing []struct {
		ID      string          `json:"id"`
		Input   string          `json:"input_hex"`
		Control string          `json:"control_hex"`
		Chunks  []int           `json:"read_chunk_sizes"`
		Decoded json.RawMessage `json:"decoded"`
		Expect  []string        `json:"expect"`
		EOF     bool            `json:"eof"`
	} `json:"framing"`
}

func loadVectors(t *testing.T) vectors {
	t.Helper()
	raw, err := os.ReadFile(vectorPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := sha256.Sum256(raw); hex.EncodeToString(got[:]) != vectorDigest {
		t.Fatal("shared vectors differ from reviewed acceptance pin")
	}
	var v vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func TestSharedFraming(t *testing.T) {
	v := loadVectors(t)
	for _, c := range v.Framing {
		t.Run(c.ID, func(t *testing.T) {
			raw, err := hex.DecodeString(c.Input)
			if err != nil {
				t.Fatal(err)
			}
			r := &chunkReader{data: raw, chunks: c.Chunks}
			got, _, err := readFrame(r)
			if c.ID == "fragmented_valid_frame" {
				want, e := decodeFrame(c.Decoded)
				if e != nil || err != nil {
					t.Fatalf("valid frame: %v / %v", err, e)
				}
				assertFrame(t, got, want)
			} else {
				want := errProtocol
				if c.EOF {
					want = errTransport
				}
				if !errors.Is(err, want) {
					t.Fatalf("decode error = %v, want %v", err, want)
				}
				if c.ID == "oversized_length" && r.read != 4 {
					t.Fatalf("read %d bytes before rejecting oversized header", r.read)
				}
			}
			if c.Control != "" {
				control, err := hex.DecodeString(c.Control)
				if err != nil {
					t.Fatal(err)
				}
				if _, _, err := readFrame(bytes.NewReader(control)); err != nil {
					t.Fatalf("independent valid control: %v", err)
				}
			}
			row := "framing/" + c.ID
			reportCheck(t, row, "input/input_hex", "internal/sut/external/codec_test.go:TestSharedFraming")
			if c.Control != "" {
				reportCheck(t, row, "control/fresh_decoder_accepts", "internal/sut/external/codec_test.go:TestSharedFraming")
			}
			if len(c.Chunks) > 0 {
				reportCheck(t, row, "input/read_chunk_sizes", "internal/sut/external/codec_test.go:chunkReader.Read")
			}
			if c.EOF {
				reportCheck(t, row, "input/eof", "internal/sut/external/codec_test.go:TestSharedFraming")
			}
			if len(c.Decoded) > 0 {
				reportCheck(t, row, "observe/decoded", "internal/sut/external/codec_test.go:assertFrame")
			}
			for i, label := range c.Expect {
				switch label {
				case "one_ready_frame_after_complete_payload":
					if got.Type != "ready" || r.read != len(raw) {
						t.Fatal("incomplete decoded frame")
					}
				case "fatal_protocol":
					if !errors.Is(err, errProtocol) {
						t.Fatal("missing protocol failure")
					}
				case "fatal_transport":
					if !errors.Is(err, errTransport) {
						t.Fatal("missing transport failure")
					}
				case "no_payload_allocation":
					if r.read != 4 || !errors.Is(err, errProtocol) {
						t.Fatal("oversized payload consumed")
					}
				case "no_dispatch":
					if err == nil || got.Type != "" {
						t.Fatal("malformed frame escaped decoder")
					}
				default:
					t.Fatalf("unhandled framing assertion %s", label)
				}
				reportCheck(t, row, fmt.Sprintf("expect/%d/%s", i, label), "internal/sut/external/codec_test.go:TestSharedFraming")
			}
		})
	}
	t.Log("EXTERNAL_SUT_FRAMING_RESULT pin=f32cd292246287a22c1a057012dd468f25c41c7d malformed=rejected control=independent")
}

type chunkReader struct {
	data   []byte
	chunks []int
	read   int
}

func (r *chunkReader) Read(p []byte) (int, error) {
	if len(r.data) == 0 {
		return 0, io.EOF
	}
	if len(r.chunks) > 0 {
		n := r.chunks[0]
		if n < len(p) {
			p = p[:n]
		}
		if n > len(p) {
			r.chunks[0] -= len(p)
		} else {
			r.chunks = r.chunks[1:]
		}
	}
	n := copy(p, r.data)
	r.data = r.data[n:]
	r.read += n
	return n, nil
}

func assertFrame(t *testing.T, got, want frame) {
	t.Helper()
	g, _ := json.Marshal(got)
	w, _ := json.Marshal(want)
	if !bytes.Equal(g, w) {
		t.Fatalf("frame mismatch: got %s want %s", g, w)
	}
}

func TestCodecStrictMatrix(t *testing.T) {
	base := `{"v":1,"type":"ready","run":"11111111111111111111111111111111","session":"22222222222222222222222222222222","seq":1,"body":{"commands":["assign"],"points":["after_read"],"capacity":2}}`
	for _, c := range []struct{ name, from, to string }{
		{"fraction", `"v":1`, `"v":1.0`}, {"exponent", `"seq":1`, `"seq":1e0`},
		{"null", `"commands":["assign"]`, `"commands":null`},
		{"unpaired_high", "assign", `\ud800`}, {"unpaired_low", "assign", `\udfff`},
		{"control", "assign", `as\u0085sign`}, {"trim", "assign", " assign"},
		{"duplicate_name", `"assign"`, `"assign","assign"`},
		{"capacity_zero", `"capacity":2`, `"capacity":0`},
		{"unknown_type", `"ready"`, `"readiness"`},
		{"negative_seq", `"seq":1`, `"seq":-1`},
		{"seq_exhaustion", `"seq":1`, `"seq":100001`},
	} {
		t.Run(c.name, func(t *testing.T) {
			if _, err := decodeFrame([]byte(strings.Replace(base, c.from, c.to, 1))); err == nil {
				t.Fatal("malformed frame accepted")
			}
		})
	}
	for _, s := range []string{`\ud83d\ude00`, `\\ud800`, "�"} {
		if _, err := decodeFrame([]byte(strings.Replace(base, "assign", s, 1))); err != nil {
			t.Fatalf("valid Unicode rejected: %v", err)
		}
	}
	f, err := decodeFrame([]byte(base))
	if err != nil {
		t.Fatal(err)
	}
	var stream bytes.Buffer
	if err := writeFrame(&stream, f); err != nil {
		t.Fatal(err)
	}
	if err := writeFrame(&stream, f); err != nil {
		t.Fatal(err)
	}
	raw := stream.Bytes()
	for _, chunk := range []int{1, len(raw)} {
		r := &chunkReader{data: bytes.Clone(raw), chunks: make([]int, len(raw))}
		for i := range r.chunks {
			r.chunks[i] = chunk
		}
		for i := 0; i < 2; i++ {
			got, _, err := readFrame(r)
			if err != nil {
				t.Fatal(err)
			}
			assertFrame(t, got, f)
		}
		if _, _, err := readFrame(r); err != io.EOF {
			t.Fatalf("boundary EOF: %v", err)
		}
	}
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], maxFrame+1)
	if _, _, err := readFrame(bytes.NewReader(header[:])); !errors.Is(err, errProtocol) {
		t.Fatal(err)
	}
}
