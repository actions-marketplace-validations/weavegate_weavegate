package io.github.weavegate.sdk;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.Statement;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.UnaryOperator;
import java.util.regex.Pattern;

import javax.sql.DataSource;

import com.zaxxer.hikari.HikariDataSource;
import org.springframework.aop.support.AopUtils;
import org.springframework.aop.support.DefaultPointcutAdvisor;
import org.springframework.aop.Advisor;
import org.springframework.aop.PointcutAdvisor;
import org.springframework.aop.framework.Advised;
import org.springframework.boot.Banner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.WebApplicationType;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.support.GenericApplicationContext;
import org.springframework.core.annotation.AnnotationUtils;
import org.springframework.core.env.MapPropertySource;
import org.springframework.scheduling.annotation.AsyncAnnotationBeanPostProcessor;
import org.springframework.scheduling.annotation.ScheduledAnnotationBeanPostProcessor;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.TransactionManager;
import org.springframework.transaction.annotation.AnnotationTransactionAttributeSource;
import org.springframework.transaction.interceptor.TransactionAttribute;
import org.springframework.transaction.interceptor.TransactionInterceptor;
import org.springframework.util.ClassUtils;
import org.springframework.util.ReflectionUtils;

/**
 * Hosts one non-web Spring Boot application for one session. The SDK owns the
 * only DataSource and transaction manager; migrations, SQL initialization,
 * web servers, scheduling and async processing are rejected or disabled.
 */
final class SpringHost implements Seams.Host {
    private static final Pattern HOST = Pattern.compile("[A-Za-z0-9._-]+|\\[[0-9A-Fa-f:.]+]");
    private static final Map<String, Object> PROPERTIES = Map.of(
            "spring.main.web-application-type", "none",
            "spring.main.banner-mode", "off",
            "spring.sql.init.mode", "never",
            "spring.flyway.enabled", "false",
            "spring.liquibase.enabled", "false");

    private record Registered(Object bean, Method method, Set<String> points) {
    }

    private final Class<?> application;
    private final String[] args;
    private final UnaryOperator<DataSource> decorate;
    private Peer peer;
    private volatile HikariDataSource pool;
    private volatile ConfigurableApplicationContext context;
    private volatile TrackingDataSource dataSource;
    private final Map<String, Registered> registered = new HashMap<>();

    SpringHost(Class<?> application, String[] args) {
        this(application, args, UnaryOperator.identity());
    }

    /** The decorator is a test seam for injecting driver failures beneath lease tracking. */
    SpringHost(Class<?> application, String[] args, UnaryOperator<DataSource> decorate) {
        this.application = application;
        this.args = args.clone();
        this.decorate = decorate;
    }

    void bind(Peer owner) {
        this.peer = owner;
    }

    @Override
    public void initialize(Seams.Start start) {
        HikariDataSource hikari = new HikariDataSource();
        hikari.setPoolName("weavegate");
        hikari.setJdbcUrl(url(start.database()));
        hikari.setUsername(start.database().username());
        hikari.setPassword(start.database().password());
        // Probe plus one lease per worker; unrelated application work is rejected, not pooled.
        hikari.setMaximumPoolSize(start.capacity() + 1);
        hikari.setMinimumIdle(0);
        hikari.setInitializationFailTimeout(-1);
        pool = hikari;
        TrackingDataSource tracked = new TrackingDataSource(decorate.apply(hikari), peer);
        dataSource = tracked;
        WeavegateTransactionManager transactions = new WeavegateTransactionManager(tracked, peer);

        SpringApplication spring = new SpringApplication(application);
        spring.setWebApplicationType(WebApplicationType.NONE);
        spring.setBannerMode(Banner.Mode.OFF);
        spring.setRegisterShutdownHook(false);
        spring.addInitializers(ctx -> {
            ctx.getEnvironment().getPropertySources().addFirst(new MapPropertySource("weavegate", PROPERTIES));
            GenericApplicationContext generic = (GenericApplicationContext) ctx;
            generic.registerBean("dataSource", DataSource.class, () -> tracked, bd -> bd.setPrimary(true));
            generic.registerBean("transactionManager", WeavegateTransactionManager.class, () -> transactions,
                    bd -> bd.setPrimary(true));
        });
        context = spring.run(args);
    }

    @Override
    public Map<String, Set<String>> validateRegistration(List<String> commands, List<String> points) {
        ConfigurableApplicationContext ctx = context;
        Map<String, DataSource> dataSources = ctx.getBeansOfType(DataSource.class);
        if (dataSources.size() != 1 || dataSources.values().iterator().next() != dataSource) {
            throw new IllegalStateException("exactly one weavegate DataSource is supported");
        }
        Map<String, TransactionManager> managers = ctx.getBeansOfType(TransactionManager.class);
        if (managers.size() != 1 || !(managers.values().iterator().next() instanceof WeavegateTransactionManager)) {
            throw new IllegalStateException("exactly one weavegate transaction manager is supported");
        }
        TransactionManager manager = managers.values().iterator().next();
        if (ctx.getBeanNamesForType(ScheduledAnnotationBeanPostProcessor.class, true, false).length > 0
                || ctx.getBeanNamesForType(AsyncAnnotationBeanPostProcessor.class, true, false).length > 0) {
            throw new IllegalStateException("scheduled and async processing are unsupported");
        }
        AnnotationTransactionAttributeSource attributes = new AnnotationTransactionAttributeSource();
        for (String name : ctx.getBeanDefinitionNames()) {
            Class<?> type = ctx.getType(name);
            if (type == null) {
                continue;
            }
            Class<?> user = java.lang.reflect.Proxy.isProxyClass(type)
                    ? AopUtils.getTargetClass(ctx.getBean(name)) : ClassUtils.getUserClass(type);
            for (Method method : ReflectionUtils.getUniqueDeclaredMethods(user)) {
                WeavegateCommand command = AnnotationUtils.findAnnotation(method, WeavegateCommand.class);
                if (command == null) {
                    continue;
                }
                Object bean = ctx.getBean(name);
                if (registered.containsKey(command.value())) {
                    throw new IllegalStateException("duplicate command registration");
                }
                if (!Wire.name(command.value()) || !Modifier.isPublic(method.getModifiers())
                        || Modifier.isStatic(method.getModifiers()) || Modifier.isFinal(method.getModifiers())
                        || method.getReturnType() != void.class
                        || method.getParameterCount() > 1
                        || (method.getParameterCount() == 1 && method.getParameterTypes()[0] != CommandContext.class)) {
                    throw new IllegalStateException("invalid command signature");
                }
                if (!AopUtils.isAopProxy(bean)) {
                    throw new IllegalStateException("command bean is not proxied");
                }
                TransactionAttribute attribute = attributes.getTransactionAttribute(method, user);
                if (!supportedTransaction(attribute)) {
                    throw new IllegalStateException(
                            "command transaction must be REQUIRED without timeout and roll back on cancellation");
                }
                Method invocable = AopUtils.selectInvocableMethod(method, bean.getClass());
                Method pointcutMethod = AopUtils.isJdkDynamicProxy(bean) ? invocable : method;
                observeCommand(bean, pointcutMethod, user, manager);
                Set<String> declared = new HashSet<>(List.of(command.points()));
                if (!declared.stream().allMatch(Wire::name)) {
                    throw new IllegalStateException("invalid point registration");
                }
                ReflectionUtils.makeAccessible(invocable);
                registered.put(command.value(), new Registered(bean, invocable, Set.copyOf(declared)));
            }
        }
        Set<String> declaredPoints = new HashSet<>();
        commands.stream().filter(registered::containsKey).forEach(c -> declaredPoints.addAll(registered.get(c).points()));
        if (!registered.keySet().containsAll(commands) || !declaredPoints.containsAll(points)) {
            throw new IllegalStateException("unsupported command or point");
        }
        Map<String, Set<String>> selected = new HashMap<>();
        commands.forEach(command -> selected.put(command, registered.get(command).points()));
        return Map.copyOf(selected);
    }

    private static void observeCommand(Object bean, Method pointcutMethod,
                                       Class<?> user, TransactionManager manager) {
        if (!(bean instanceof Advised advised) || advised.isFrozen()) {
            throw new IllegalStateException("command proxy must expose its transaction advice");
        }
        Advisor[] advisors = advised.getAdvisors();
        int transaction = -1;
        int observer = -1;
        for (int i = 0; i < advisors.length; i++) {
            Advisor advisor = advisors[i];
            if (!matchesCommand(advisor, pointcutMethod, user)) {
                continue;
            }
            if (advisor.getAdvice() instanceof FailureObserver) {
                if (observer != -1) {
                    throw new IllegalStateException("duplicate command failure observer");
                }
                observer = i;
            }
            if (advisor.getAdvice() instanceof TransactionInterceptor interceptor) {
                if (advisor instanceof PointcutAdvisor pointcut) {
                    if (pointcut.getPointcut().getMethodMatcher().isRuntime()) {
                        throw new IllegalStateException("runtime transaction pointcuts are unsupported");
                    }
                }
                TransactionManager configured = interceptor.getTransactionManager();
                if (transaction != -1 || interceptor.getTransactionAttributeSource() == null
                        // A null manager resolves by type; the context check above makes that
                        // the one SDK manager. A directly configured manager must be identical.
                        || (configured != null && configured != manager)
                        || !supportedTransaction(interceptor.getTransactionAttributeSource().getTransactionAttribute(pointcutMethod, user))) {
                    throw new IllegalStateException("command requires one matching weavegate transaction advice");
                }
                transaction = i;
            }
        }
        if (transaction == -1) {
            throw new IllegalStateException("command lacks transaction advice");
        }
        if (observer != -1) {
            if (observer < transaction) {
                throw new IllegalStateException("command failure observer must be inside transaction advice");
            }
            for (int i = transaction + 1; i < observer; i++) {
                if (matchesCommand(advisors[i], pointcutMethod, user)) {
                    throw new IllegalStateException("command failure observer must be inside transaction advice");
                }
            }
        }
        if (observer == -1) {
            // Inside the transaction interceptor: observe a body failure before
            // Spring rolls back or returns the lease. Spring keeps all decisions.
            if (advisors[transaction] instanceof PointcutAdvisor pointcut) {
                advised.addAdvisor(transaction + 1,
                        new DefaultPointcutAdvisor(pointcut.getPointcut(), new FailureObserver()));
            } else {
                advised.addAdvice(transaction + 1, new FailureObserver());
            }
        }
    }

    private static boolean matchesCommand(Advisor advisor, Method method, Class<?> user) {
        if (!(advisor instanceof PointcutAdvisor pointcut)) {
            return true;
        }
        // Spring excludes static nonmatches from this command's effective chain.
        return pointcut.getPointcut().getClassFilter().matches(user)
                && pointcut.getPointcut().getMethodMatcher().matches(method, user);
    }

    private static boolean supportedTransaction(TransactionAttribute attribute) {
        return attribute != null && attribute.getPropagationBehavior() == TransactionDefinition.PROPAGATION_REQUIRED
                && attribute.getTimeout() == TransactionDefinition.TIMEOUT_DEFAULT
                && attribute.rollbackOn(new WeavegateCancelledException("validation"))
                && (attribute.getQualifier() == null || attribute.getQualifier().isEmpty());
    }

    @Override
    public void probeDatabase() throws Exception {
        try (Connection connection = dataSource.getConnection(); Statement statement = connection.createStatement()) {
            statement.execute("SELECT 1");
        }
    }

    @Override
    public void cancelStartup() {
        // SpringApplication.run cannot be interrupted safely; the peer's startup/stop watchdogs bound it.
    }

    @Override
    public void execute(CommandContext command) throws Throwable {
        Registered target = registered.get(command.command());
        try {
            if (target.method().getParameterCount() == 0) {
                target.method().invoke(target.bean());
            } else {
                target.method().invoke(target.bean(), command);
            }
        } catch (InvocationTargetException e) {
            throw e.getCause();
        }
    }

    @Override
    public void close() {
        ConfigurableApplicationContext ctx = context;
        if (ctx != null) {
            ctx.close();
        }
        HikariDataSource hikari = pool;
        if (hikari != null) {
            hikari.close();
            if (!hikari.isClosed()) {
                throw new IllegalStateException("pool did not close");
            }
        }
    }

    static String url(Seams.Database database) {
        if (!HOST.matcher(database.host()).matches()) {
            throw new IllegalArgumentException("unsupported database host");
        }
        String name = URLEncoder.encode(database.name(), StandardCharsets.UTF_8).replace("+", "%20");
        return "jdbc:mysql://" + database.host() + ":" + database.port() + "/" + name;
    }
}
