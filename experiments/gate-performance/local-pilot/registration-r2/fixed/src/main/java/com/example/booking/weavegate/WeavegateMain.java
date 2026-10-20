package com.example.booking.weavegate;

import com.example.booking.BookingApplication;
import io.github.weavegate.sdk.WeavegateChild;

/** Entry point of the weavegate test JAR; the weavegate CLI launches it as a child JVM. */
public final class WeavegateMain {
    private WeavegateMain() {
    }

    public static void main(String[] args) {
        WeavegateChild.run(BookingApplication.class, args);
    }
}
