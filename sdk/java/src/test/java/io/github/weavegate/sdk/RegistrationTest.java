package io.github.weavegate.sdk;

import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import java.util.concurrent.atomic.AtomicReference;
import java.util.stream.Stream;
import javax.sql.DataSource;

import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.TestFactory;
import org.springframework.aop.framework.ProxyFactory;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.scheduling.annotation.AsyncAnnotationBeanPostProcessor;
import org.springframework.scheduling.annotation.ScheduledAnnotationBeanPostProcessor;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.transaction.TransactionManager;
import org.springframework.transaction.annotation.AnnotationTransactionAttributeSource;
import org.springframework.transaction.annotation.EnableTransactionManagement;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.interceptor.DefaultTransactionAttribute;
import org.springframework.transaction.interceptor.TransactionInterceptor;

class RegistrationTest {
    @Configuration(proxyBeanMethods = false)
    @EnableTransactionManagement(proxyTargetClass = true)
    static class Transactions { }

    public static class FinalCommands {
        @Transactional @WeavegateCommand("bad")
        public final void bad() { throw new AssertionError("must not dispatch"); }
        @Transactional @WeavegateCommand("good")
        public void good() { }
    }

    public static class StaticCommands {
        @Transactional @WeavegateCommand("bad")
        public static void bad() { throw new AssertionError("must not dispatch"); }
        @Transactional @WeavegateCommand("good")
        public void good() { }
    }

    public static class Commands {
        @Transactional @WeavegateCommand(value = "selected", points = "selected_point")
        public void selected() { }
        @Transactional @WeavegateCommand(value = "other", points = "other_point")
        public void other() { }
    }

    public static class TimeoutCommands {
        @Transactional(timeout = 1) @WeavegateCommand("timed")
        public void timed() { }
    }

    public static class AsyncCommands {
        @Transactional @WeavegateCommand("future")
        public java.util.concurrent.Future<Void> future() { return new java.util.concurrent.CompletableFuture<>(); }
        @Transactional @WeavegateCommand("stage")
        public java.util.concurrent.CompletionStage<Void> stage() { return new java.util.concurrent.CompletableFuture<>(); }
    }

    public interface CommandApi { void selected(); }

    public static class InterfaceCommands implements CommandApi {
        @Transactional @WeavegateCommand("selected")
        public void selected() { }
    }

    @TestFactory
    Stream<DynamicTest> asynchronousCommandsFailRegistration() {
        return RequirementsTest.repeated(() ->
                assertThatThrownBy(() -> validate(AsyncCommands.class, List.of("future", "stage"), List.of()))
                        .isInstanceOf(IllegalStateException.class).hasMessage("invalid command signature"));
    }

    @TestFactory
    Stream<DynamicTest> jdkProxyDiscoversImplementationCommands() {
        return RequirementsTest.repeated(() -> validateCustomProxy(true));
    }

    @TestFactory
    Stream<DynamicTest> jdkProxyRejectsPointcutThatMatchesOnlyImplementationMethod() {
        return RequirementsTest.repeated(() -> {
            VectorHarness harness = new VectorHarness("independent").quiet();
            TrackingDataSource dataSource = new TrackingDataSource(new DriverManagerDataSource(), harness.peer);
            WeavegateTransactionManager manager = new WeavegateTransactionManager(dataSource, harness.peer);
            ProxyFactory factory = new ProxyFactory(new InterfaceCommands());
            factory.setProxyTargetClass(false);
            var implementationAnnotation = new org.springframework.aop.support.StaticMethodMatcherPointcut() {
                @Override public boolean matches(java.lang.reflect.Method method, Class<?> type) {
                    return method.isAnnotationPresent(Transactional.class);
                }
            };
            factory.addAdvisor(new org.springframework.aop.support.DefaultPointcutAdvisor(implementationAnnotation,
                    new TransactionInterceptor((TransactionManager) manager,
                            new AnnotationTransactionAttributeSource())));
            Object proxy = factory.getProxy();
            // Spring sees the unannotated interface method at this invocation,
            // so the transaction interceptor is absent and this call returns.
            ((CommandApi) proxy).selected();
            SpringHost host = new SpringHost(Transactions.class, new String[0]);
            host.bind(harness.peer);
            try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
                context.registerBean("dataSource", DataSource.class, () -> dataSource);
                context.registerBean("transactionManager", WeavegateTransactionManager.class, () -> manager);
                context.registerBean("commands", Object.class, () -> proxy);
                context.refresh();
                ReflectionTestUtils.setField(host, "context", context);
                ReflectionTestUtils.setField(host, "dataSource", dataSource);
                assertThatThrownBy(() -> host.validateRegistration(List.of("selected"), List.of()))
                        .isInstanceOf(IllegalStateException.class)
                        .hasMessage("command lacks transaction advice");
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> jdkProxyRejectsAttributesMissingFromInvokedMethod() {
        return RequirementsTest.repeated(() -> {
            VectorHarness harness = new VectorHarness("independent").quiet();
            TrackingDataSource dataSource = new TrackingDataSource(new DriverManagerDataSource(), harness.peer);
            WeavegateTransactionManager manager = new WeavegateTransactionManager(dataSource, harness.peer);
            AtomicReference<java.lang.reflect.Method> runtimeMethod = new AtomicReference<>();
            var attributes = (org.springframework.transaction.interceptor.TransactionAttributeSource) (method, type) -> {
                runtimeMethod.set(method);
                return method.getDeclaringClass() == InterfaceCommands.class
                        ? new DefaultTransactionAttribute() : null;
            };
            ProxyFactory factory = new ProxyFactory(new InterfaceCommands());
            factory.setProxyTargetClass(false);
            factory.addAdvice(new TransactionInterceptor((TransactionManager) manager, attributes));
            Object proxy = factory.getProxy();
            ((CommandApi) proxy).selected();
            assertThat(runtimeMethod.get().getDeclaringClass()).isEqualTo(CommandApi.class);

            SpringHost host = new SpringHost(Transactions.class, new String[0]);
            host.bind(harness.peer);
            try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
                context.registerBean("dataSource", DataSource.class, () -> dataSource);
                context.registerBean("transactionManager", WeavegateTransactionManager.class, () -> manager);
                context.registerBean("commands", Object.class, () -> proxy);
                context.refresh();
                ReflectionTestUtils.setField(host, "context", context);
                ReflectionTestUtils.setField(host, "dataSource", dataSource);
                assertThatThrownBy(() -> host.validateRegistration(List.of("selected"), List.of()))
                        .isInstanceOf(IllegalStateException.class)
                        .hasMessage("command requires one matching weavegate transaction advice");
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> unrelatedTransactionAdviceDoesNotRejectCommand() {
        return RequirementsTest.repeated(() -> validateCustomProxy(false));
    }

    @TestFactory
    Stream<DynamicTest> separateStaticTransactionAdvisorsObserveEachCommand() {
        return RequirementsTest.repeated(() -> {
            VectorHarness harness = new VectorHarness("independent").quiet();
            TrackingDataSource dataSource = new TrackingDataSource(new DriverManagerDataSource(), harness.peer);
            WeavegateTransactionManager manager = new WeavegateTransactionManager(dataSource, harness.peer);
            ProxyFactory factory = new ProxyFactory(new Commands());
            factory.setProxyTargetClass(true);
            for (String name : List.of("selected", "other")) {
                var pointcut = new org.springframework.aop.support.StaticMethodMatcherPointcut() {
                    @Override public boolean matches(java.lang.reflect.Method method, Class<?> type) {
                        return method.getName().equals(name);
                    }
                };
                factory.addAdvisor(new org.springframework.aop.support.DefaultPointcutAdvisor(pointcut,
                        new TransactionInterceptor((TransactionManager) manager,
                                new AnnotationTransactionAttributeSource())));
            }
            Commands proxy = (Commands) factory.getProxy();
            SpringHost host = new SpringHost(Transactions.class, new String[0]);
            host.bind(harness.peer);
            try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
                context.registerBean("dataSource", DataSource.class, () -> dataSource);
                context.registerBean("transactionManager", WeavegateTransactionManager.class, () -> manager);
                context.registerBean("commands", Commands.class, () -> proxy);
                context.refresh();
                ReflectionTestUtils.setField(host, "context", context);
                ReflectionTestUtils.setField(host, "dataSource", dataSource);
                assertThat(host.validateRegistration(List.of("selected", "other"), List.of()))
                        .containsKeys("selected", "other");
                for (String name : List.of("selected", "other")) {
                    var method = Commands.class.getMethod(name);
                    var chain = java.util.Arrays.stream(((org.springframework.aop.framework.Advised) proxy).getAdvisors())
                            .filter(advisor -> !(advisor instanceof org.springframework.aop.PointcutAdvisor pointcut)
                                    || pointcut.getPointcut().getMethodMatcher().matches(method, Commands.class))
                            .toList();
                    int transaction = -1;
                    int observers = 0;
                    for (int i = 0; i < chain.size(); i++) {
                        if (chain.get(i).getAdvice() instanceof TransactionInterceptor) {
                            transaction = i;
                        }
                        if (chain.get(i).getAdvice() instanceof FailureObserver) {
                            observers++;
                        }
                    }
                    assertThat(observers).isEqualTo(1);
                    assertThat(transaction).isGreaterThanOrEqualTo(0);
                    assertThat(chain.get(transaction + 1).getAdvice()).isInstanceOf(FailureObserver.class);
                }
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> failureObserverMustBeImmediatelyInsideTransaction() {
        return RequirementsTest.repeated(() -> {
            VectorHarness harness = new VectorHarness("independent").quiet();
            TrackingDataSource dataSource = new TrackingDataSource(new DriverManagerDataSource(), harness.peer);
            WeavegateTransactionManager manager = new WeavegateTransactionManager(dataSource, harness.peer);
            ProxyFactory factory = new ProxyFactory(new InterfaceCommands());
            factory.addAdvice(new FailureObserver());
            factory.addAdvice(new TransactionInterceptor((TransactionManager) manager,
                    new AnnotationTransactionAttributeSource()));
            Object proxy = factory.getProxy();
            SpringHost host = new SpringHost(Transactions.class, new String[0]);
            host.bind(harness.peer);
            try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
                context.registerBean("dataSource", DataSource.class, () -> dataSource);
                context.registerBean("transactionManager", WeavegateTransactionManager.class, () -> manager);
                context.registerBean("commands", Object.class, () -> proxy);
                context.refresh();
                ReflectionTestUtils.setField(host, "context", context);
                ReflectionTestUtils.setField(host, "dataSource", dataSource);
                assertThatThrownBy(() -> host.validateRegistration(List.of("selected"), List.of()))
                        .isInstanceOf(IllegalStateException.class)
                        .hasMessage("command failure observer must be inside transaction advice");
            }
        });
    }

    private static void validateCustomProxy(boolean jdk) {
        VectorHarness harness = new VectorHarness("independent").quiet();
        TrackingDataSource dataSource = new TrackingDataSource(new DriverManagerDataSource(), harness.peer);
        WeavegateTransactionManager manager = new WeavegateTransactionManager(dataSource, harness.peer);
        ProxyFactory factory = new ProxyFactory(new InterfaceCommands());
        factory.setProxyTargetClass(!jdk);
        if (!jdk) {
            var unrelated = new org.springframework.aop.support.StaticMethodMatcherPointcut() {
                @Override public boolean matches(java.lang.reflect.Method method, Class<?> type) {
                    return method.getName().equals("toString");
                }
            };
            factory.addAdvisor(new org.springframework.aop.support.DefaultPointcutAdvisor(unrelated,
                    new TransactionInterceptor((TransactionManager) manager, new AnnotationTransactionAttributeSource())));
        }
        factory.addAdvice(new TransactionInterceptor((TransactionManager) manager,
                new AnnotationTransactionAttributeSource()));
        var selected = new org.springframework.aop.support.StaticMethodMatcherPointcut() {
            @Override public boolean matches(java.lang.reflect.Method method, Class<?> type) {
                return method.getName().equals("selected");
            }
        };
        var commandAdvice = new org.springframework.aop.support.DefaultPointcutAdvisor(selected,
                (org.aopalliance.intercept.MethodInterceptor) invocation -> invocation.proceed());
        factory.addAdvisor(commandAdvice);
        Object proxy = factory.getProxy();
        SpringHost host = new SpringHost(Transactions.class, new String[0]);
        host.bind(harness.peer);
        try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
            context.registerBean("dataSource", DataSource.class, () -> dataSource);
            context.registerBean("transactionManager", WeavegateTransactionManager.class, () -> manager);
            context.registerBean("commands", Object.class, () -> proxy);
            context.refresh();
            ReflectionTestUtils.setField(host, "context", context);
            ReflectionTestUtils.setField(host, "dataSource", dataSource);
            assertThat(host.validateRegistration(List.of("selected"), List.of())).containsKey("selected");
            var advisors = ((org.springframework.aop.framework.Advised) proxy).getAdvisors();
            int transaction = -1;
            for (int i = 0; i < advisors.length; i++) {
                if (advisors[i].getAdvice() instanceof TransactionInterceptor
                        && advisors[i] != commandAdvice) {
                    transaction = i;
                }
            }
            assertThat(transaction).isGreaterThanOrEqualTo(0);
            assertThat(advisors[transaction + 1].getAdvice()).isInstanceOf(FailureObserver.class);
            assertThat(advisors[transaction + 2]).isSameAs(commandAdvice);
        }
    }

    static void validate(Class<?> commands, List<String> selected, List<String> points) {
        validate(commands, selected, points, null);
    }

    static void validate(Class<?> commands, List<String> selected, List<String> points, Class<?> processor) {
        VectorHarness harness = new VectorHarness("independent").quiet();
        TrackingDataSource dataSource = new TrackingDataSource(new DriverManagerDataSource(), harness.peer);
        SpringHost host = new SpringHost(Transactions.class, new String[0]);
        host.bind(harness.peer);
        try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
            context.registerBean("dataSource", DataSource.class, () -> dataSource);
            context.registerBean("transactionManager", WeavegateTransactionManager.class,
                    () -> new WeavegateTransactionManager(dataSource, harness.peer));
            context.register(Transactions.class, commands);
            if (processor != null) {
                context.registerBean("customProcessor", processor);
            }
            context.refresh();
            ReflectionTestUtils.setField(host, "context", context);
            ReflectionTestUtils.setField(host, "dataSource", dataSource);
            host.validateRegistration(selected, points);
        }
    }

    @TestFactory
    Stream<DynamicTest> unadvisableCommandsFailRegistration() {
        return RequirementsTest.repeated(() -> {
            for (Class<?> type : List.of(FinalCommands.class, StaticCommands.class)) {
                assertThatThrownBy(() -> validate(type, List.of("bad"), List.of()))
                        .isInstanceOf(IllegalStateException.class);
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> pointsBelongToSelectedCommands() {
        return RequirementsTest.repeated(() -> {
            validate(Commands.class, List.of("selected"), List.of("selected_point"));
            assertThatThrownBy(() -> validate(Commands.class, List.of("selected"), List.of("other_point")))
                    .isInstanceOf(IllegalStateException.class);
        });
    }

    @TestFactory
    Stream<DynamicTest> schedulingProcessorsAreRejectedByType() {
        return RequirementsTest.repeated(() -> {
            for (Class<?> processor : List.of(ScheduledAnnotationBeanPostProcessor.class,
                    AsyncAnnotationBeanPostProcessor.class)) {
                assertThatThrownBy(() -> validate(Commands.class, List.of("selected"), List.of(), processor))
                        .isInstanceOf(IllegalStateException.class);
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> wallClockTransactionTimeoutsAreRejected() {
        return RequirementsTest.repeated(() ->
                assertThatThrownBy(() -> validate(TimeoutCommands.class, List.of("timed"), List.of()))
                        .isInstanceOf(IllegalStateException.class));
    }

    @TestFactory
    Stream<DynamicTest> commandAdviceMustUseRegisteredTransactionManager() {
        return RequirementsTest.repeated(() -> {
            VectorHarness harness = new VectorHarness("independent").quiet();
            TrackingDataSource dataSource = new TrackingDataSource(new DriverManagerDataSource(), harness.peer);
            WeavegateTransactionManager registered = new WeavegateTransactionManager(dataSource, harness.peer);
            WeavegateTransactionManager separate = new WeavegateTransactionManager(dataSource, harness.peer);
            ProxyFactory factory = new ProxyFactory(new Commands());
            factory.setProxyTargetClass(true);
            factory.addAdvice(new TransactionInterceptor(
                    (TransactionManager) separate, new AnnotationTransactionAttributeSource()));
            Commands proxy = (Commands) factory.getProxy();
            SpringHost host = new SpringHost(Transactions.class, new String[0]);
            host.bind(harness.peer);
            try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
                context.registerBean("dataSource", DataSource.class, () -> dataSource);
                context.registerBean("transactionManager", WeavegateTransactionManager.class, () -> registered);
                context.registerBean("commands", Commands.class, () -> proxy);
                context.refresh();
                ReflectionTestUtils.setField(host, "context", context);
                ReflectionTestUtils.setField(host, "dataSource", dataSource);
                assertThatThrownBy(() -> host.validateRegistration(List.of("selected"), List.of("selected_point")))
                        .isInstanceOf(IllegalStateException.class);
            }
        });
    }
}
