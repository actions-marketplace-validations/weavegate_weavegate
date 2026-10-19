package io.github.weavegate.fixture.springquota;

import io.github.weavegate.sdk.WeavegateChild;

public final class InstrumentedMain {
    private InstrumentedMain() {
    }

    public static void main(String[] args) {
        WeavegateChild.run(QuotaApplication.class, args);
    }
}
