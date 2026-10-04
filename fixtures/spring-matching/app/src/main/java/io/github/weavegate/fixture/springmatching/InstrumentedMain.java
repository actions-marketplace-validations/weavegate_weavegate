package io.github.weavegate.fixture.springmatching;

import io.github.weavegate.sdk.WeavegateChild;

/** Entrypoint of the instrumented test JAR; the weavegate CLI launches it as its child JVM. */
public final class InstrumentedMain {
    private InstrumentedMain() {
    }

    public static void main(String[] args) {
        WeavegateChild.run(MatchingApplication.class, args);
    }
}
