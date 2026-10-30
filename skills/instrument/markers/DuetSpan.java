// Duet Instrumentation span marker. Copy unchanged into base and head (adjust
// the package line). Writes {"span","ns"} lines to $DUET_SPAN_FILE; does
// nothing without it.
import java.io.FileWriter;
import java.io.IOException;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

public final class DuetSpan implements AutoCloseable {
    private static final String FILE = System.getenv("DUET_SPAN_FILE");
    private static final StringBuilder BUF = new StringBuilder();
    private static int count = 0;
    private static final DuetSpan NOOP = new DuetSpan(null);

    static {
        if (FILE != null) {
            var ex = Executors.newSingleThreadScheduledExecutor(r -> {
                var t = new Thread(r, "duet-span-flush");
                t.setDaemon(true);
                return t;
            });
            ex.scheduleAtFixedRate(DuetSpan::flush, 1, 1, TimeUnit.SECONDS);
            Runtime.getRuntime().addShutdownHook(new Thread(DuetSpan::flush));
        }
    }

    private final String name;
    private final long t0;

    private DuetSpan(String name) {
        this.name = name;
        this.t0 = name == null ? 0 : System.nanoTime();
    }

    public static DuetSpan start(String name) {
        return FILE == null ? NOOP : new DuetSpan(name);
    }

    @Override
    public void close() {
        if (name == null) return;
        long ns = System.nanoTime() - t0;
        boolean full;
        synchronized (BUF) {
            BUF.append("{\"span\":\"").append(name).append("\",\"ns\":").append(ns).append("}\n");
            full = ++count >= 512;
        }
        if (full) flush();
    }

    private static void flush() {
        String out;
        synchronized (BUF) {
            if (BUF.length() == 0) return;
            out = BUF.toString();
            BUF.setLength(0);
            count = 0;
        }
        try (var w = new FileWriter(FILE, true)) {
            w.write(out);
        } catch (IOException ignored) {
        }
    }
}
