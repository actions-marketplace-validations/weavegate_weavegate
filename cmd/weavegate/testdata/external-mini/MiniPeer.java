import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

// A tiny independent JVM for the CLI composition test. It speaks only the
// one-worker success history used there; Spring and JDBC acceptance are #111.
public final class MiniPeer {
    private static final DataInputStream IN = new DataInputStream(System.in);
    private static final DataOutputStream OUT = new DataOutputStream(System.out);
    private static String run;
    private static String session;
    private static int seq;

    private static String field(String json, String key) {
        Matcher match = Pattern.compile("\\\"" + key + "\\\"\\s*:\\s*\\\"([^\\\"]+)\\\"").matcher(json);
        if (!match.find()) throw new IllegalArgumentException("missing field " + key);
        return match.group(1);
    }

    private static void send(String type, String body) throws Exception {
        String json = "{\"v\":1,\"type\":\"" + type + "\",\"run\":\"" + run
            + "\",\"session\":\"" + session + "\",\"seq\":" + (++seq) + ",\"body\":" + body + "}";
        byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
        OUT.writeInt(bytes.length);
        OUT.write(bytes);
        OUT.flush();
    }

    public static void main(String[] args) throws Exception {
        String invocation = null;
        String worker = null;
        while (true) {
            int length = IN.readInt();
            if (length < 1 || length > 1_048_576) throw new IllegalArgumentException("frame length");
            String frame = new String(IN.readNBytes(length), StandardCharsets.UTF_8);
            String type = field(frame, "type");
            if (type.equals("start")) {
                run = field(frame, "run");
                session = field(frame, "session");
                Files.writeString(Path.of(field(frame, "wire_log")), run + " " + session + "\n",
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND);
                field(frame, "password");
                if (!frame.contains("\"commands\":[\"ping\"]") || !frame.contains("\"points\":[\"at\"]"))
                    throw new IllegalArgumentException("registration");
                send("ready", "{\"commands\":[\"ping\"],\"points\":[\"at\"],\"capacity\":1}");
            } else if (type.equals("invoke")) {
                invocation = field(frame, "invocation");
                worker = field(frame, "worker");
                String binding = "\"invocation\":\"" + invocation + "\",\"worker\":\"" + worker + "\"";
                send("accepted", "{" + binding + "}");
                send("arrive", "{" + binding + ",\"arrival\":\"1\",\"point\":\"at\"}");
            } else if (type.equals("release")) {
                send("terminal", "{\"invocation\":\"" + invocation + "\",\"worker\":\"" + worker
                    + "\",\"transaction\":\"committed\",\"connection\":\"returned\",\"error\":null}");
            } else if (type.equals("stop")) {
                send("stopped", "{}");
                return;
            } else {
                throw new IllegalArgumentException("unexpected frame type " + type);
            }
        }
    }
}
