"""Provider-neutral intent routing and evidence planning contracts.

The planning layer is deliberately deterministic.  Repository source is never
sent to a classifier, and no provider response is trusted as routing metadata.
The contracts in this module are shared by routing, planned retrieval, prompt
construction, and safe API serialization.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from sourcetrace.generation.client import GenerationMessage
from sourcetrace.models.domain import RetrievedEvidence


class IntentName(StrEnum):
    """Small, extensible set of repository question intents."""

    REPOSITORY_OVERVIEW = "repository_overview"
    ARCHITECTURE = "architecture"
    ENTRYPOINT_AND_STARTUP = "entrypoint_and_startup"
    SYMBOL_OR_FILE_EXPLANATION = "symbol_or_file_explanation"
    BEHAVIOR_OR_DATA_FLOW = "behavior_or_data_flow"
    IMPACT_AND_CHANGE = "impact_and_change"
    CONFIGURATION_AND_SETUP = "configuration_and_setup"
    TESTING_AND_QUALITY = "testing_and_quality"
    DEPENDENCY_AND_INTEGRATION = "dependency_and_integration"
    GENERAL_CHAT = "general_chat"
    ACKNOWLEDGEMENT = "acknowledgement"
    UNKNOWN_REPOSITORY_QUESTION = "unknown_repository_question"


class RetrievalMethod(StrEnum):
    """Evidence acquisition methods understood by the planned executor."""

    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    GRAPH = "graph"
    METADATA = "metadata"


class SourceCategory(StrEnum):
    """Safe source categories used for ranking and user-facing metadata."""

    README_DOCS = "readme_docs"
    MANIFESTS = "manifests"
    ENTRYPOINTS = "entrypoints"
    ROUTES = "routes"
    SERVICES = "services"
    STORAGE = "storage"
    TESTS = "tests"
    CONFIGURATION = "configuration"
    RELATIONSHIPS = "relationships"
    CODE = "code"


# These are the shared query-time safety limits.  Keeping them here prevents
# one route or prompt builder from silently widening the evidence surface.
MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_CHARS = 4_800
MAX_QUERY_VARIANTS = 6
MAX_SUB_QUESTIONS = 4
MAX_DIRECT_RESULTS_PER_QUERY = 6
MAX_EVIDENCE_ITEMS = 8
MAX_EXPANDED_FILES = 8
MAX_EXPANDED_CHUNKS = 16
MAX_GRAPH_HOPS = 2
MAX_GRAPH_SEEDS = 2
MAX_PROMPT_CHARS = 16_000
MAX_ANSWER_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class PlanningHints:
    """Non-sensitive lexical hints extracted from a user question."""

    paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    technologies: tuple[str, ...] = ()
    flow_nouns: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IntentClassification:
    """Validated intent result with no private reasoning or raw provider data."""

    primary_intent: IntentName
    secondary_intents: tuple[IntentName, ...] = ()
    confidence: float = 0.0
    is_repository_question: bool = True
    is_general_chat: bool = False
    is_acknowledgement: bool = False
    hints: PlanningHints = PlanningHints()

    def __post_init__(self) -> None:
        if not isinstance(self.primary_intent, IntentName):
            raise ValueError("Invalid intent classification.")
        if type(self.confidence) is not float or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Invalid intent confidence.")

    @property
    def confidence_bucket(self) -> str:
        if self.confidence >= 0.85:
            return "high"
        if self.confidence >= 0.60:
            return "medium"
        return "low"


@dataclass(frozen=True, slots=True)
class EvidencePlan:
    """Bounded, provider-neutral instructions for collecting evidence."""

    intent: IntentName
    query_variants: tuple[str, ...]
    retrieval_methods: tuple[RetrievalMethod, ...]
    source_categories: tuple[SourceCategory, ...]
    requested_hop_depth: int = 0
    max_evidence_items: int = MAX_EVIDENCE_ITEMS
    max_expanded_files: int = MAX_EXPANDED_FILES
    max_expanded_chunks: int = MAX_EXPANDED_CHUNKS
    overview_allowed: bool = False
    citations_required: bool = True
    sub_questions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.requested_hop_depth < 0 or self.requested_hop_depth > MAX_GRAPH_HOPS:
            raise ValueError("Invalid evidence hop depth.")
        if not 1 <= self.max_evidence_items <= MAX_EVIDENCE_ITEMS:
            raise ValueError("Invalid evidence item limit.")
        if not 1 <= self.max_expanded_files <= MAX_EXPANDED_FILES:
            raise ValueError("Invalid expansion file limit.")
        if not 1 <= self.max_expanded_chunks <= MAX_EXPANDED_CHUNKS:
            raise ValueError("Invalid expansion chunk limit.")


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Normalized selected evidence plus safe provenance metadata."""

    items: tuple[RetrievedEvidence, ...] = ()
    retrieval_methods: tuple[RetrievalMethod, ...] = ()
    deduplication_keys: tuple[str, ...] = ()
    expansion_hops: int = 0
    source_categories: tuple[SourceCategory, ...] = ()
    plan: EvidencePlan | None = None
    classification: IntentClassification | None = None
    reindex_required: bool = False


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The single internal route a message takes before retrieval."""

    classification: IntentClassification
    plan: EvidencePlan | None = None


_PATH_PATTERN = re.compile(
    r"(?<![\w])(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+"
    r"|(?<![\w])[A-Za-z0-9_.-]+\.(?:py|pyi|js|jsx|ts|tsx|json|toml|yaml|yml|md|rst|txt|sql|sh)\b"
)
_BACKTICK_PATTERN = re.compile(r"`([^`\r\n]{1,120})`")
_NAMED_SYMBOL_PATTERN = re.compile(
    r"\b(?:function|class|method|symbol|handler|endpoint|route|module|component)\s+"
    r"([A-Za-z_][A-Za-z0-9_.:-]{1,100})",
    re.IGNORECASE,
)
_CAMEL_OR_IDENTIFIER_PATTERN = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9]+[A-Z][A-Za-z0-9]*|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b"
)

_TECHNOLOGIES = (
    "python",
    "javascript",
    "typescript",
    "react",
    "vite",
    "fastapi",
    "flask",
    "django",
    "express",
    "node",
    "mongodb",
    "postgres",
    "sql",
    "docker",
    "github",
)
_FLOW_NOUNS = (
    "auth",
    "authentication",
    "startup",
    "routing",
    "storage",
    "database",
    "ingestion",
    "indexing",
    "retrieval",
    "rendering",
    "request",
    "response",
    "payment",
    "event",
    "queue",
    "configuration",
)
_OPERATIONS = (
    "explain",
    "trace",
    "compare",
    "modify",
    "change",
    "test",
    "configure",
    "setup",
    "impact",
    "find",
)


def _unique(values: Iterable[str], *, limit: int = 12) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = value.strip().strip("`'\".,;:!?()[]{}")
        if not clean or len(clean) > 120:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if len(result) >= limit:
            break
    return tuple(result)


def extract_planning_hints(question: str) -> PlanningHints:
    """Extract bounded lexical hints without interpreting repository content."""
    paths = _unique(match.group(0) for match in _PATH_PATTERN.finditer(question))
    symbols: list[str] = []
    symbols.extend(match.group(1) for match in _BACKTICK_PATTERN.finditer(question))
    symbols.extend(match.group(1) for match in _NAMED_SYMBOL_PATTERN.finditer(question))
    symbols.extend(match.group(0) for match in _CAMEL_OR_IDENTIFIER_PATTERN.finditer(question))
    for path in paths:
        symbols.append(path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0])

    lowered = question.casefold()
    return PlanningHints(
        paths=paths,
        symbols=_unique(symbols),
        technologies=tuple(
            term for term in _TECHNOLOGIES if re.search(rf"\b{re.escape(term)}\b", lowered)
        ),
        flow_nouns=tuple(
            term for term in _FLOW_NOUNS if re.search(rf"\b{re.escape(term)}\b", lowered)
        ),
        operations=tuple(
            term for term in _OPERATIONS if re.search(rf"\b{re.escape(term)}\b", lowered)
        ),
    )


def _normalized_tokens(question: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_-]*", question.casefold()))


def _is_acknowledgement(question: str) -> bool:
    normalized = re.sub(r"[^a-z0-9']+", " ", question.casefold()).strip()
    if not normalized or len(normalized.split()) > 12:
        return False
    patterns = (
        r"^(?:oh+\s+)?(?:it'?s|that'?s)?\s*(?:look(?:s|ing)?|seem(?:s)?)?\s*"
        r"(?:good|great|nice|cool|perfect|fine|okay|ok|helpful|clear)$",
        r"^(?:ok(?:ay)?|alright|got it|understood|makes sense|thank you|thanks)$",
        r"^(?:oh+|ah+|wow)\s+(?:okay|ok|nice|great|good|cool)$",
    )
    return any(re.fullmatch(pattern, normalized) for pattern in patterns)


def _is_clearly_general_chat(question: str) -> bool:
    normalized = re.sub(r"[^a-z0-9'?!]+", " ", question.casefold()).strip()
    if not normalized:
        return False
    patterns = (
        r"^(?:how do i|how can i) (?:cook|make) .+[?!]?$",
        r"^(?:what'?s|what is) the weather(?: today| now)?[?!]?$",
        r"^(?:tell me|give me) a joke[?!]?$",
        r"^(?:what|who) is the (?:capital|president|population) of .+[?!]?$",
        r"^(?:translate|define|explain) .+ (?:to|in) (?:english|khmer|french|spanish)[?!]?$",
        r"^(?:what are|give me) today'?s news[?!]?$",
    )
    return any(re.fullmatch(pattern, normalized) for pattern in patterns)


def _contains_code_signal(question: str, hints: PlanningHints) -> bool:
    if hints.paths or hints.symbols or hints.technologies:
        return True
    return bool(
        re.search(
            r"[/\\._-]|\b(?:file|function|class|method|component|variable|module|folder|package|"
            r"import|export|api|endpoint|route|database|query|test|config|hook|auth|login|user|"
            r"data|storage|state|prop|render|deploy|build|error|bug|fix|refactor|change|update|"
            r"add|remove|delete|create|modify|code|script|server|client|frontend|backend|index|main|app)\b",
            question,
            re.IGNORECASE,
        )
    )


def _recent_user_subject(
    conversation_context: Sequence[GenerationMessage] | None,
) -> str | None:
    if not conversation_context:
        return None
    user_questions = [
        message.content.strip()
        for message in conversation_context[-MAX_HISTORY_MESSAGES:]
        if isinstance(message, GenerationMessage)
        and message.role == "user"
        and isinstance(message.content, str)
        and message.content.strip()
    ]
    if not user_questions:
        return None
    return " ".join(user_questions[-2:])[-1_800:]


class IntentRouter:
    """Conservative deterministic router for repository conversations."""

    def classify(
        self,
        question: str,
        conversation_context: Sequence[GenerationMessage] | None = None,
    ) -> IntentClassification:
        if type(question) is not str or not question.strip():
            raise ValueError("Invalid question for intent routing.")

        clean = question.strip()
        hints = extract_planning_hints(clean)
        if _is_acknowledgement(clean):
            return IntentClassification(
                primary_intent=IntentName.ACKNOWLEDGEMENT,
                confidence=0.99,
                is_repository_question=False,
                is_general_chat=False,
                is_acknowledgement=True,
                hints=hints,
            )
        if _is_clearly_general_chat(clean):
            return IntentClassification(
                primary_intent=IntentName.GENERAL_CHAT,
                confidence=0.99,
                is_repository_question=False,
                is_general_chat=True,
                hints=hints,
            )

        tokens = _normalized_tokens(clean)
        lower = clean.casefold()
        secondary: list[IntentName] = []

        if (
            "overview" in tokens
            or "organization" in tokens
            or "organized" in tokens
            or "read" in tokens
            and "first" in tokens
            or "what does" in lower
            and ("repository" in lower or "codebase" in lower)
            or "what is this" in lower
        ):
            primary = IntentName.REPOSITORY_OVERVIEW
            confidence = 0.94
        elif any(term in tokens for term in ("architecture", "structure", "boundaries", "layers")):
            primary = IntentName.ARCHITECTURE
            confidence = 0.95
            if any(term in tokens for term in ("flow", "data", "request")):
                secondary.append(IntentName.BEHAVIOR_OR_DATA_FLOW)
        elif any(
            term in tokens for term in ("entrypoint", "bootstrap", "startup", "launch", "boot")
        ) or (
            any(term in tokens for term in ("start", "begin", "run", "running", "main"))
            and any(term in tokens for term in ("where", "application", "app", "server", "code"))
        ):
            primary = IntentName.ENTRYPOINT_AND_STARTUP
            confidence = 0.96
        elif (
            any(
                term in tokens for term in ("impact", "affected", "affect", "dependents", "callers")
            )
            or "change safely" in lower
            or "what would happen if" in lower
        ):
            primary = IntentName.IMPACT_AND_CHANGE
            confidence = 0.95
        elif any(
            term in tokens
            for term in ("configure", "configuration", "setup", "install", "environment")
        ):
            primary = IntentName.CONFIGURATION_AND_SETUP
            confidence = 0.91
        elif any(
            term in tokens for term in ("test", "tests", "testing", "pytest", "coverage", "quality")
        ):
            primary = IntentName.TESTING_AND_QUALITY
            confidence = 0.92
        elif any(
            term in tokens
            for term in (
                "integrate",
                "integration",
                "provider",
                "external",
                "connect",
                "dependency",
            )
        ):
            primary = IntentName.DEPENDENCY_AND_INTEGRATION
            confidence = 0.90
        elif (
            hints.paths
            or hints.symbols
            or any(
                term in tokens
                for term in ("file", "function", "class", "method", "symbol", "module")
            )
        ) and any(term in tokens for term in ("what", "how", "explain", "does", "do")):
            primary = IntentName.SYMBOL_OR_FILE_EXPLANATION
            confidence = 0.93
        elif any(
            term in tokens
            for term in (
                "flow",
                "trace",
                "moves",
                "pass",
                "request",
                "event",
                "auth",
                "authentication",
                "payment",
                "storage",
                "database",
                "routing",
                "ingestion",
                "retrieval",
                "rendering",
            )
        ):
            primary = IntentName.BEHAVIOR_OR_DATA_FLOW
            confidence = 0.90
        elif _contains_code_signal(clean, hints):
            primary = IntentName.UNKNOWN_REPOSITORY_QUESTION
            confidence = 0.64
        elif self._is_contextual_follow_up(clean, conversation_context):
            # Short follow-ups such as "why?", "can we change it?", or
            # "explain more" need the previous repository subject.  Keep these
            # in repository mode so context-aware retrieval can resolve them.
            primary = IntentName.UNKNOWN_REPOSITORY_QUESTION
            confidence = 0.72
        else:
            # A real natural-language question with no repository signal is
            # ordinary chat.  Preserve ambiguous non-question text as a
            # repository query for backwards-compatible callers and fixtures
            # that pass labels such as "Question".
            question_marked = "?" in clean or bool(
                re.match(
                    r"^(?:what|who|where|when|why|how|can|could|should|would|is|are|do|does|did|will)\\b",
                    lower,
                )
            )
            if question_marked:
                primary = IntentName.GENERAL_CHAT
                confidence = 0.86
            else:
                primary = IntentName.UNKNOWN_REPOSITORY_QUESTION
                confidence = 0.64

        if primary == IntentName.GENERAL_CHAT:
            return IntentClassification(
                primary_intent=primary,
                secondary_intents=tuple(dict.fromkeys(secondary)),
                confidence=float(confidence),
                is_repository_question=False,
                is_general_chat=True,
                hints=hints,
            )

        if primary in (IntentName.ARCHITECTURE, IntentName.BEHAVIOR_OR_DATA_FLOW):
            if "auth" in hints.flow_nouns or "authentication" in hints.flow_nouns:
                secondary.append(IntentName.DEPENDENCY_AND_INTEGRATION)
        return IntentClassification(
            primary_intent=primary,
            secondary_intents=tuple(dict.fromkeys(secondary)),
            confidence=float(confidence),
            is_repository_question=True,
            hints=hints,
        )

    @staticmethod
    def _is_contextual_follow_up(
        question: str,
        conversation_context: Sequence[GenerationMessage] | None,
    ) -> bool:
        """Keep short referential follow-ups attached to a repository subject."""
        subject = _recent_user_subject(conversation_context)
        if not subject:
            return False
        if not _contains_code_signal(subject, extract_planning_hints(subject)):
            return False

        normalized = re.sub(r"[^a-z0-9']+", " ", question.casefold()).strip()
        words = normalized.split()
        if not words or len(words) > 10:
            return False

        referential_terms = {
            "it",
            "that",
            "this",
            "them",
            "those",
            "same",
            "more",
            "there",
        }
        if referential_terms.intersection(words):
            return True
        return bool(
            re.match(
                r"^(?:why|how|what about|can we|should we|is it|does it|which one|and|.+\\s+what)\\b",
                normalized,
            )
        )

    def route(
        self,
        question: str,
        conversation_context: Sequence[GenerationMessage] | None = None,
    ) -> RouteDecision:
        classification = self.classify(question, conversation_context)
        if not classification.is_repository_question:
            return RouteDecision(classification=classification)
        return RouteDecision(
            classification=classification,
            plan=build_evidence_plan(question, classification, conversation_context),
        )

    @staticmethod
    def has_code_signal(question: str) -> bool:
        return _contains_code_signal(question, extract_planning_hints(question))

    @staticmethod
    def is_acknowledgement(question: str) -> bool:
        return _is_acknowledgement(question)

    @staticmethod
    def is_clearly_general_chat(question: str) -> bool:
        return _is_clearly_general_chat(question)


def decompose_question(question: str, hints: PlanningHints) -> tuple[str, ...]:
    """Split only explicit multi-ask questions and cap the result."""
    clean = question.strip()
    parts = [p.strip(" \t\r\n?;:") for p in re.split(r"\?+|;", clean) if p.strip()]
    if len(parts) <= 1:
        parts = [
            p.strip()
            for p in re.split(
                r"\s+(?:and|also)\s+(?=(?:where|how|what|which|why|can|does|do)\b)",
                clean,
                flags=re.IGNORECASE,
            )
            if p.strip()
        ]

    if (
        len(parts) <= 1
        and len(hints.flow_nouns) > 1
        and any(term in hints.operations for term in ("trace", "explain", "find"))
    ):
        parts = [f"{clean} Focus on {noun}." for noun in hints.flow_nouns[:MAX_SUB_QUESTIONS]]

    meaningful = [p for p in parts if len(p.split()) >= 3]
    if len(meaningful) <= 1:
        return ()
    return _unique(meaningful, limit=MAX_SUB_QUESTIONS)


_INTENT_TERMS: dict[IntentName, tuple[str, ...]] = {
    IntentName.REPOSITORY_OVERVIEW: ("README", "manifest", "entry point", "top-level modules"),
    IntentName.ARCHITECTURE: ("architecture", "modules", "boundaries", "data flow"),
    IntentName.ENTRYPOINT_AND_STARTUP: ("main", "app", "server", "bootstrap", "entry point"),
    IntentName.SYMBOL_OR_FILE_EXPLANATION: ("definition", "imports", "callers", "containing file"),
    IntentName.BEHAVIOR_OR_DATA_FLOW: ("source", "transformation", "storage", "output", "flow"),
    IntentName.IMPACT_AND_CHANGE: ("callers", "dependents", "imports", "tests", "integration"),
    IntentName.CONFIGURATION_AND_SETUP: (
        "README",
        "manifest",
        "scripts",
        "configuration",
        "environment",
    ),
    IntentName.TESTING_AND_QUALITY: (
        "tests",
        "test configuration",
        "package.json pyproject.toml pytest.ini tox.ini setup.cfg",
        "vitest.config.ts jest.config.js test scripts",
        "scripts",
        "fixtures",
        "coverage",
    ),
    IntentName.DEPENDENCY_AND_INTEGRATION: (
        "imports",
        "providers",
        "routes",
        "services",
        "storage",
    ),
    IntentName.UNKNOWN_REPOSITORY_QUESTION: ("relevant source", "definitions", "references"),
}

_PLAN_METHODS: dict[IntentName, tuple[RetrievalMethod, ...]] = {
    IntentName.REPOSITORY_OVERVIEW: (
        RetrievalMethod.METADATA,
        RetrievalMethod.LEXICAL,
        RetrievalMethod.SEMANTIC,
        RetrievalMethod.GRAPH,
    ),
    IntentName.ARCHITECTURE: (
        RetrievalMethod.METADATA,
        RetrievalMethod.LEXICAL,
        RetrievalMethod.SEMANTIC,
        RetrievalMethod.GRAPH,
    ),
    IntentName.ENTRYPOINT_AND_STARTUP: (
        RetrievalMethod.LEXICAL,
        RetrievalMethod.METADATA,
        RetrievalMethod.GRAPH,
        RetrievalMethod.SEMANTIC,
    ),
    IntentName.SYMBOL_OR_FILE_EXPLANATION: (
        RetrievalMethod.LEXICAL,
        RetrievalMethod.METADATA,
        RetrievalMethod.SEMANTIC,
        RetrievalMethod.GRAPH,
    ),
    IntentName.BEHAVIOR_OR_DATA_FLOW: (
        RetrievalMethod.LEXICAL,
        RetrievalMethod.SEMANTIC,
        RetrievalMethod.GRAPH,
    ),
    IntentName.IMPACT_AND_CHANGE: (
        RetrievalMethod.LEXICAL,
        RetrievalMethod.GRAPH,
        RetrievalMethod.METADATA,
        RetrievalMethod.SEMANTIC,
    ),
    IntentName.CONFIGURATION_AND_SETUP: (
        RetrievalMethod.LEXICAL,
        RetrievalMethod.METADATA,
        RetrievalMethod.SEMANTIC,
    ),
    IntentName.TESTING_AND_QUALITY: (
        RetrievalMethod.LEXICAL,
        RetrievalMethod.METADATA,
        RetrievalMethod.SEMANTIC,
    ),
    IntentName.DEPENDENCY_AND_INTEGRATION: (
        RetrievalMethod.LEXICAL,
        RetrievalMethod.SEMANTIC,
        RetrievalMethod.GRAPH,
        RetrievalMethod.METADATA,
    ),
    IntentName.UNKNOWN_REPOSITORY_QUESTION: (
        RetrievalMethod.SEMANTIC,
        RetrievalMethod.LEXICAL,
        RetrievalMethod.GRAPH,
    ),
}

_PLAN_CATEGORIES: dict[IntentName, tuple[SourceCategory, ...]] = {
    IntentName.REPOSITORY_OVERVIEW: (
        SourceCategory.README_DOCS,
        SourceCategory.MANIFESTS,
        SourceCategory.ENTRYPOINTS,
        SourceCategory.SERVICES,
        SourceCategory.ROUTES,
        SourceCategory.RELATIONSHIPS,
    ),
    IntentName.ARCHITECTURE: (
        SourceCategory.ENTRYPOINTS,
        SourceCategory.MANIFESTS,
        SourceCategory.ROUTES,
        SourceCategory.SERVICES,
        SourceCategory.STORAGE,
        SourceCategory.CONFIGURATION,
        SourceCategory.RELATIONSHIPS,
    ),
    IntentName.ENTRYPOINT_AND_STARTUP: (
        SourceCategory.ENTRYPOINTS,
        SourceCategory.MANIFESTS,
        SourceCategory.CONFIGURATION,
        SourceCategory.ROUTES,
    ),
    IntentName.SYMBOL_OR_FILE_EXPLANATION: (
        SourceCategory.CODE,
        SourceCategory.SERVICES,
        SourceCategory.ROUTES,
        SourceCategory.TESTS,
    ),
    IntentName.BEHAVIOR_OR_DATA_FLOW: (
        SourceCategory.ENTRYPOINTS,
        SourceCategory.ROUTES,
        SourceCategory.SERVICES,
        SourceCategory.STORAGE,
        SourceCategory.RELATIONSHIPS,
    ),
    IntentName.IMPACT_AND_CHANGE: (
        SourceCategory.CODE,
        SourceCategory.SERVICES,
        SourceCategory.ROUTES,
        SourceCategory.TESTS,
        SourceCategory.CONFIGURATION,
        SourceCategory.RELATIONSHIPS,
    ),
    IntentName.CONFIGURATION_AND_SETUP: (
        SourceCategory.README_DOCS,
        SourceCategory.MANIFESTS,
        SourceCategory.CONFIGURATION,
        SourceCategory.TESTS,
    ),
    IntentName.TESTING_AND_QUALITY: (
        SourceCategory.TESTS,
        SourceCategory.MANIFESTS,
        SourceCategory.README_DOCS,
        SourceCategory.CONFIGURATION,
    ),
    IntentName.DEPENDENCY_AND_INTEGRATION: (
        SourceCategory.MANIFESTS,
        SourceCategory.ROUTES,
        SourceCategory.SERVICES,
        SourceCategory.STORAGE,
        SourceCategory.CONFIGURATION,
        SourceCategory.RELATIONSHIPS,
    ),
    IntentName.UNKNOWN_REPOSITORY_QUESTION: (
        SourceCategory.CODE,
        SourceCategory.README_DOCS,
        SourceCategory.SERVICES,
    ),
}


def build_evidence_plan(
    question: str,
    classification: IntentClassification,
    conversation_context: Sequence[GenerationMessage] | None = None,
) -> EvidencePlan:
    """Create a bounded plan from safe question and conversation hints."""
    intent = classification.primary_intent
    hints = classification.hints
    sub_questions = decompose_question(question, hints)
    recent_subject = _recent_user_subject(conversation_context)
    needs_subject = bool(recent_subject) and (
        len(question.split()) <= 8
        or bool(re.search(r"\b(it|that|this|they|them|there|same)\b", question, re.IGNORECASE))
    )

    variants: list[str] = [question.strip()]
    if needs_subject and recent_subject:
        variants.append(f"{question.strip()} Subject context: {recent_subject}")
    variants.extend(sub_questions)
    if hints.paths or hints.symbols:
        variants.append(" ".join((*hints.paths, *hints.symbols)))
    variants.append(" ".join(_INTENT_TERMS.get(intent, ())))

    # Testing questions need explicit manifest/runner names. A generic query
    # such as "tests scripts coverage" can rank build-tool configuration
    # above the files that actually define test commands.
    if intent == IntentName.TESTING_AND_QUALITY:
        variants.extend(
            (
                "package.json test scripts vitest jest",
                "pyproject.toml pytest.ini tox.ini setup.cfg pytest testpaths",
                "vitest.config.ts vitest.config.js jest.config.ts jest.config.js",
                "tests test fixtures coverage",
            )
        )

    query_variants = _unique(variants, limit=MAX_QUERY_VARIANTS)
    graph_intents = {
        IntentName.ARCHITECTURE,
        IntentName.BEHAVIOR_OR_DATA_FLOW,
        IntentName.IMPACT_AND_CHANGE,
        IntentName.DEPENDENCY_AND_INTEGRATION,
        IntentName.REPOSITORY_OVERVIEW,
    }
    hop_depth = MAX_GRAPH_HOPS if intent in graph_intents else 0
    return EvidencePlan(
        intent=intent,
        query_variants=query_variants,
        retrieval_methods=_PLAN_METHODS.get(
            intent, _PLAN_METHODS[IntentName.UNKNOWN_REPOSITORY_QUESTION]
        ),
        source_categories=_PLAN_CATEGORIES.get(
            intent, _PLAN_CATEGORIES[IntentName.UNKNOWN_REPOSITORY_QUESTION]
        ),
        requested_hop_depth=hop_depth,
        max_evidence_items=MAX_EVIDENCE_ITEMS,
        max_expanded_files=MAX_EXPANDED_FILES,
        max_expanded_chunks=MAX_EXPANDED_CHUNKS,
        overview_allowed=intent in (IntentName.REPOSITORY_OVERVIEW, IntentName.ARCHITECTURE),
        citations_required=True,
        sub_questions=sub_questions,
    )


def classify_source_category(relative_path: str, symbol_name: str = "") -> SourceCategory:
    """Classify a repository-relative source location for ranking only."""
    path = relative_path.replace("\\", "/").casefold()
    name = symbol_name.casefold()
    filename = path.rsplit("/", 1)[-1]
    if (
        filename.startswith(("readme", "changelog", "contributing"))
        or "/docs/" in f"/{path}/"
        or path.endswith((".md", ".rst", ".txt"))
    ):
        return SourceCategory.README_DOCS
    if filename in {
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "cargo.toml",
        "go.mod",
        "dockerfile",
        "makefile",
    } or filename.endswith((".lock",)):
        return SourceCategory.MANIFESTS
    if any(term in path or term in name for term in ("test", "spec", "fixture")):
        return SourceCategory.TESTS
    if any(
        term in path or term in name
        for term in ("config", "setting", "env", "deploy", "docker", "compose")
    ):
        return SourceCategory.CONFIGURATION
    if any(
        term in path or term in name
        for term in ("route", "router", "endpoint", "controller", "handler", "api")
    ):
        return SourceCategory.ROUTES
    if any(
        term in path or term in name
        for term in ("storage", "repository", "database", "db", "model", "schema")
    ):
        return SourceCategory.STORAGE
    if any(term in path or term in name for term in ("service", "usecase", "domain", "worker")):
        return SourceCategory.SERVICES
    if any(
        term in filename or term in name
        for term in ("main", "app", "index", "server", "cli", "bootstrap", "entry")
    ):
        return SourceCategory.ENTRYPOINTS
    return SourceCategory.CODE


def answer_mode_for_intent(intent: IntentName) -> str:
    return {
        IntentName.REPOSITORY_OVERVIEW: "orientation",
        IntentName.ARCHITECTURE: "architecture",
        IntentName.ENTRYPOINT_AND_STARTUP: "normal",
        IntentName.SYMBOL_OR_FILE_EXPLANATION: "normal",
        IntentName.BEHAVIOR_OR_DATA_FLOW: "flow",
        IntentName.IMPACT_AND_CHANGE: "impact",
        IntentName.CONFIGURATION_AND_SETUP: "normal",
        IntentName.TESTING_AND_QUALITY: "normal",
        IntentName.DEPENDENCY_AND_INTEGRATION: "architecture",
        IntentName.UNKNOWN_REPOSITORY_QUESTION: "normal",
        IntentName.GENERAL_CHAT: "general_chat",
        IntentName.ACKNOWLEDGEMENT: "conversation",
    }.get(intent, "normal")


def safe_planning_metadata(
    classification: IntentClassification | None,
    bundle: EvidenceBundle | None,
) -> dict[str, object]:
    """Return only bounded, non-sensitive metadata suitable for an API response."""
    if classification is None:
        return {}
    plan = bundle.plan if bundle else None
    items = bundle.items if bundle else ()
    return {
        "intent": classification.primary_intent.value,
        "confidence_bucket": classification.confidence_bucket,
        "evidence_count": len(items),
        "hop_count": int(bundle.expansion_hops) if bundle else 0,
        "source_categories": [
            category.value for category in (bundle.source_categories if bundle else ())
        ],
        "sub_question_count": len(plan.sub_questions) if plan else 0,
    }
