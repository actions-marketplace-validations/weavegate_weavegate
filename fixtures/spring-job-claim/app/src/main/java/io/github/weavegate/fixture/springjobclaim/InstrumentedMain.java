package io.github.weavegate.fixture.springjobclaim;

import io.github.weavegate.sdk.WeavegateChild;

public final class InstrumentedMain {
    private InstrumentedMain() {
    }

    public static void main(String[] args) {
        WeavegateChild.run(JobClaimApplication.class, args);
    }
}
