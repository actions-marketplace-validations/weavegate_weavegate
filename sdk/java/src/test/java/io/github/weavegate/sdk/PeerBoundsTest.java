package io.github.weavegate.sdk;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/** Independently written deadline regressions outside the shared vector inventory. */
class PeerBoundsTest {
    @Test
    void failedAcceptanceRetiresUndispatchedInvocationBeforeCleanup() {
        for (boolean sequenceExhausted : new boolean[] {false, true}) {
            VectorHarness h = Scripted.ready(1);
            if (sequenceExhausted) {
                h.peer.sent = Wire.MAX_SEQ;
            } else {
                h.output.closed = true;
            }
            h.host.shutdown.signal();
            h.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
            h.activity.awaitIdle();
            Peer.Invocation invocation = h.peer.invocations.get(Scripted.I1);
            assertThat(invocation.proxy).isEqualTo(Peer.Proxy.SKIPPED);
            assertThat(invocation.retired).isTrue();
            assertThat(invocation.dispatches).isZero();
            assertThat(h.peer.live).isZero();
            assertThat(h.host.closeCompleted).isTrue();
            assertThat(h.exit.status()).isEqualTo(1);
        }
    }

    @Test
    void fatalCleanupBoundSurvivesRetirementOfTheInvocationThatOwnedIt() {
        VectorHarness h = Scripted.ready(2);
        h.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
        h.peer.receive(Scripted.invoke(3, Scripted.I2, "w2", "assign"));
        h.clock.advanceTo(100);
        h.peer.receive(Scripted.cancel(4, Scripted.I1, "w1", "context"));
        h.clock.advanceTo(200);
        // A protocol fault while I1's cancellation deadline (1100 ms) is active.
        h.peer.receive(Scripted.release(5, Scripted.I2, "w2", "1", "after_read"));
        assertThat(h.peer.fatalKind).isEqualTo("protocol");
        assertThat(h.peer.fatalDeadline).isEqualTo(1100);

        // I1 retires cleanly; I2 never completes cleanup.
        Scripted.complete(h, Scripted.I1, Peer.Transaction.ROLLED_BACK);
        assertThat(h.peer.invocations.get(Scripted.I1).retired).isTrue();
        h.clock.advanceTo(1099);
        assertThat(h.exit.status()).isNull();
        h.clock.advanceTo(1100);
        assertThat(h.exit.status()).isEqualTo(1);
    }

    @Test
    void stopDuringReadinessProbeLeaseIsNotAnOutsideLease() {
        VectorHarness h = new VectorHarness("independent").quiet();
        h.host.shutdown.signal();
        h.peer.receive(Scripted.start(1));
        h.activity.awaitIdle();
        assertThat(h.host.probeStarted).isTrue();
        // Stop before the probe returns; the probe lease was acquired before readiness.
        h.peer.receive(Scripted.stop(2, 2500));
        h.activity.awaitIdle();
        assertThat(h.peer.fatalKind).isNull();
        assertThat(h.output.frames).extracting(f -> f.get("type").stringValue()).containsExactly("stopped");
        assertThat(h.exit.status()).isZero();

        // After readiness, a lease without an invocation is still a session failure.
        VectorHarness ready = Scripted.ready(1);
        ready.peer.leaseAcquired(null);
        assertThat(ready.peer.fatalKind).isEqualTo("protocol");
    }

    @Test
    void fatalWithoutDeadlineUsesCancelBudgetFromDetection() {
        VectorHarness h = Scripted.ready(1);
        h.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
        h.clock.advanceTo(40);
        h.peer.receive(Scripted.invoke(3, Scripted.I1, "w1", "assign"));
        assertThat(h.peer.fatalDeadline).isEqualTo(1040);
        h.clock.advanceTo(1039);
        assertThat(h.exit.status()).isNull();
        h.clock.advanceTo(1040);
        assertThat(h.exit.status()).isEqualTo(1);
    }
}
