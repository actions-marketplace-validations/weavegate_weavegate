package io.github.weavegate.experiment;

import com.zaxxer.hikari.HikariDataSource;
import io.github.weavegate.sdk.CommandContext;
import io.github.weavegate.sdk.Weavegate;
import io.github.weavegate.sdk.WeavegateCommand;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.springframework.aop.support.AopUtils;
import org.springframework.boot.Banner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.WebApplicationType;
import org.springframework.context.support.GenericApplicationContext;
import org.springframework.core.env.MapPropertySource;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.annotation.AnnotationTransactionAttributeSource;
import org.springframework.transaction.TransactionDefinition;
import javax.sql.DataSource;

/** Experiment entrypoint only. Business classes and the SDK are unchanged. */
public final class BaselineMain {
    private record Command(Object proxy, Method method) {}

    public static void main(String[] args) throws Exception {
        PrintStream events = System.out;
        System.setOut(System.err);
        var input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        Map<String, String> values = new HashMap<>();
        for (String line; (line = input.readLine()) != null && !line.isEmpty();) {
            int split = line.indexOf('=');
            if (split < 1) throw new IllegalArgumentException("invalid private input");
            values.put(line.substring(0, split), new String(Base64.getDecoder().decode(
                    line.substring(split + 1)), StandardCharsets.UTF_8));
        }
        if (Weavegate.active()) throw new IllegalStateException("hooks must be inactive");
        Weavegate.syncPoint("inactive-probe");
        try (var pool = new HikariDataSource()) {
            pool.setPoolName("weavegate");
            pool.setJdbcUrl(values.get("url"));
            pool.setUsername(values.get("username"));
            pool.setPassword(values.get("password"));
            pool.setMaximumPoolSize(3); // Product capacity 2 plus its probe lease.
            pool.setMinimumIdle(0);
            pool.setInitializationFailTimeout(-1);
            var transactions = new JdbcTransactionManager(pool);
            var app = new SpringApplication(Class.forName(args[0]));
            app.setWebApplicationType(WebApplicationType.NONE);
            app.setBannerMode(Banner.Mode.OFF);
            app.setRegisterShutdownHook(false);
            app.addInitializers(ctx -> {
                ctx.getEnvironment().getPropertySources().addFirst(new MapPropertySource("e5", Map.of(
                        "spring.main.web-application-type", "none", "spring.main.banner-mode", "off",
                        "spring.sql.init.mode", "never", "spring.flyway.enabled", "false",
                        "spring.liquibase.enabled", "false")));
                var generic = (GenericApplicationContext) ctx;
                generic.registerBean("dataSource", DataSource.class, () -> pool, bd -> bd.setPrimary(true));
                generic.registerBean("transactionManager", JdbcTransactionManager.class,
                        () -> transactions, bd -> bd.setPrimary(true));
            });
            try (var context = app.run()) {
                Map<String, Command> commands = new HashMap<>();
                var attributes = new AnnotationTransactionAttributeSource();
                for (String name : context.getBeanDefinitionNames()) {
                    Class<?> type = context.getType(name);
                    if (type == null || !type.getName().startsWith("io.github.weavegate.fixture.")) continue;
                    Object bean = context.getBean(name);
                    Class<?> target = AopUtils.getTargetClass(bean);
                    for (Method method : target.getMethods()) {
                        var annotation = method.getAnnotation(WeavegateCommand.class);
                        if (annotation == null) continue;
                        var tx = attributes.getTransactionAttribute(method, target);
                        if (!AopUtils.isAopProxy(bean) || tx == null
                                || tx.getPropagationBehavior() != TransactionDefinition.PROPAGATION_REQUIRED
                                || (tx.getIsolationLevel() != TransactionDefinition.ISOLATION_REPEATABLE_READ
                                    && tx.getIsolationLevel() != TransactionDefinition.ISOLATION_DEFAULT))
                            throw new IllegalStateException("original REQUIRED/RR proxy is required");
                        if (commands.put(annotation.value(), new Command(bean,
                                AopUtils.selectInvocableMethod(method, bean.getClass()))) != null)
                            throw new IllegalStateException("duplicate command");
                    }
                }
                try (Connection probe = pool.getConnection(); var statement = probe.createStatement();
                     var rows = statement.executeQuery("SELECT @@session.transaction_isolation")) {
                    if (!rows.next() || !"REPEATABLE-READ".equals(rows.getString(1)))
                        throw new IllegalStateException("unexpected session isolation");
                }
                Map<String, String> params = new HashMap<>();
                values.forEach((k, v) -> { if (k.startsWith("arg.")) params.put(k.substring(4), v); });
                var start = new CountDownLatch(1);
                try (var workers = Executors.newFixedThreadPool(2)) {
                    var futures = new ArrayList<Future<?>>();
                    for (int i = 0; i < 2; i++) {
                        String worker = values.get("worker." + i), command = values.get("command." + i);
                        Command registered = commands.get(command);
                        if (registered == null) throw new IllegalStateException("missing original command");
                        var invocation = new CommandContext("e5-" + i, worker, command, values.get("variant"), params);
                        futures.add(workers.submit(() -> {
                            start.await();
                            registered.method().invoke(registered.proxy(), invocation);
                            return null; // Proxy return follows commit and connection return.
                        }));
                    }
                    events.println("{\"event\":\"ready\",\"hooks_active\":false,\"proxies\":2,\"pool_max\":3,\"isolation\":\"REPEATABLE-READ\"}");
                    if (!"GO".equals(input.readLine())) throw new IllegalStateException("missing dispatch");
                    start.countDown(); // The only latch, before either proxy invocation.
                    for (Future<?> future : futures) future.get();
                }
                if (pool.getHikariPoolMXBean().getActiveConnections() != 0)
                    throw new IllegalStateException("connection lease remains after proxy return");
                events.println("{\"event\":\"done\",\"workers\":2,\"active_connections\":0,\"hooks_active\":false}");
                if (!"STOP".equals(input.readLine())) throw new IllegalStateException("missing stop");
            }
        }
        events.println("{\"event\":\"closed\"}");
    }
}
