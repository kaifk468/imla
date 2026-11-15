"""Java / Spring Boot focus: packs, replacements, hint budget, prompt (offline)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imla import format as fmt  # noqa: E402
from imla import vocabpacks  # noqa: E402
from imla.config import (  # noqa: E402
    default_config, get_dictionary, get_effective_dictionary, get_vocabulary,
)
from imla.dictionary import apply_dictionary, compile_dictionary  # noqa: E402
from imla.transcribe import MAX_VOCAB_CHARS, build_initial_prompt  # noqa: E402

# (what Whisper tends to write, expected after the dictionary)
CASES = [
    ("we use spring boot with spring data jpa", "we use Spring Boot with Spring Data JPA"),
    ("add at rest controller to the class", "add @RestController to the class"),
    ("mark the method at transactional", "mark the method @Transactional"),
    ("inject it with at autowired", "inject it with @Autowired"),
    ("use at get mapping for the endpoint", "use @GetMapping for the endpoint"),
    ("the at request body is missing", "the @RequestBody is missing"),
    ("at path variable and at request param", "@PathVariable and @RequestParam"),
    ("at spring boot test with at mock bean", "@SpringBootTest with @MockBean"),
    ("the at spring boot application class", "the @SpringBootApplication class"),
    ("it throws a null pointer exception", "it throws a NullPointerException"),
    ("an illegal argument exception was thrown", "an IllegalArgumentException was thrown"),
    ("the j p a repository returns nothing", "the JpaRepository returns nothing"),
    ("map the entity with j p a", "map the entity with JPA"),
    ("we run j unit and mockito tests", "we run JUnit and Mockito tests"),
    ("mock ito stubs the call", "Mockito stubs the call"),
    ("edit the pom dot xml", "edit the pom.xml"),
    ("change application dot yml", "change application.yml"),
    ("set it in application dot properties", "set it in application.properties"),
    ("use lombok to cut boilerplate", "use Lombok to cut boilerplate"),
    ("hibernate lazy loading", "Hibernate lazy loading"),
    ("a concurrent hash map and an array list", "a ConcurrentHashMap and an ArrayList"),
    ("completable future with an executor service", "CompletableFuture with an ExecutorService"),
    ("flyway migrations on postgres sql", "Flyway migrations on PostgreSQL"),
    ("test containers with kafka", "Testcontainers with Kafka"),
    ("rabbit mq and redis", "RabbitMQ and Redis"),
    ("secure it with o auth 2 and j w t", "secure it with OAuth2 and JWT"),
    ("run it on the j v m of j d k 21", "run it on the JVM of JDK 21"),
    ("upgrade to java 21", "upgrade to Java 21"),
    ("fix the n plus one problem", "fix the N+1 problem"),
    ("the d t o and the p o j o", "the DTO and the POJO"),
    ("use the resilience 4 j circuit breaker", "use the Resilience4j circuit breaker"),
    ("add a feign client", "add a Feign client"),
    ("key cloak handles login", "Keycloak handles login"),
    ("deploy to kubernetes with docker file", "deploy to Kubernetes with Dockerfile"),
    ("send the j son payload", "send the JSON payload"),
    ("open the pull request on get hub", "open the pull request on GitHub"),
    ("run kube control get pods", "run kubectl get pods"),
    ("the c i c d pipeline", "the CI/CD pipeline"),
    ("open intelli j", "open IntelliJ"),
    ("at override the method", "@Override the method"),
    ("the server runs on port eighty eighty", "the server runs on port 8080"),
]


def _apply(text, cfg=None):
    cfg = cfg or default_config()
    return apply_dictionary(text, compile_dictionary(get_effective_dictionary(cfg)))


class ReplacementTests(unittest.TestCase):
    def test_cases(self):
        for spoken, expected in CASES:
            with self.subTest(spoken=spoken):
                self.assertEqual(_apply(spoken), expected)

    def test_does_not_touch_plain_english(self):
        for text in ("we should look at the service", "he went to the store",
                     "the meeting is at three", "look at it carefully"):
            self.assertEqual(_apply(text), text)


class PackTests(unittest.TestCase):
    def test_packs_load(self):
        self.assertGreater(len(vocabpacks.pack_terms("java_spring")), 150)
        self.assertGreater(len(vocabpacks.pack_terms("software_general")), 50)
        self.assertGreater(len(vocabpacks.pack_replacements("java_spring")), 100)

    def test_no_backslash_in_replacements(self):
        for name in vocabpacks.PACK_ORDER:
            for _, written in vocabpacks.pack_replacements(name):
                self.assertNotIn("\\", written)

    def test_user_dictionary_excludes_pack_entries(self):
        cfg = default_config()
        self.assertEqual(len(get_dictionary(cfg)), len(cfg["dictionary"]))
        self.assertGreater(len(get_effective_dictionary(cfg)), len(get_dictionary(cfg)) + 100)

    def test_user_entry_overrides_pack(self):
        cfg = default_config()
        cfg["dictionary"] = [["spring boot", "SpringBoot"]]
        self.assertEqual(_apply("a spring boot app", cfg), "a SpringBoot app")

    def test_packs_can_be_disabled(self):
        cfg = default_config()
        cfg["vocab_packs"] = {"java_spring": False, "software_general": False}
        self.assertEqual(_apply("at transactional", cfg), "at transactional")
        self.assertNotIn("@Transactional", get_vocabulary(cfg))

    def test_user_vocabulary_comes_first(self):
        cfg = default_config()
        cfg["vocabulary"] = ["MyInternalService"]
        self.assertEqual(get_vocabulary(cfg)[0], "MyInternalService")

    def test_java_terms_precede_general_terms(self):
        v = get_vocabulary(default_config())
        self.assertLess(v.index("Spring Boot"), v.index("GraphQL"))


class HintAndPromptTests(unittest.TestCase):
    def test_whisper_hint_within_budget_and_java_first(self):
        p = build_initial_prompt(get_vocabulary(default_config()))
        self.assertLessEqual(len(p), MAX_VOCAB_CHARS + len("Technical terms: ") + 2)
        self.assertIn("Spring Boot", p)
        self.assertIn("@Transactional", p)

    def test_prompt_developer_focus_toggle(self):
        on = fmt.build_system_prompt(developer_focus=True)
        off = fmt.build_system_prompt(developer_focus=False)
        self.assertIn("Java and Spring Boot", on)
        self.assertIn("@Autowired", on)
        self.assertNotIn("Java and Spring Boot", off)
        for p in (on, off):
            self.assertNotIn("{developer", p)

    def test_rule_tier_keeps_java_terms(self):
        res = fmt.clean_transcript(
            "um mark the method at transactional and use j unit",
            tier_override="rule",
            dictionary=get_effective_dictionary(default_config()),
        )
        self.assertIn("@Transactional", res.text)
        self.assertIn("JUnit", res.text)


if __name__ == "__main__":
    unittest.main()
