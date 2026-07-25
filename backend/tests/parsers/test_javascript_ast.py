"""
Offline unit tests for JavaScript and TypeScript static AST parsing.
"""

from pathlib import Path

from sourcetrace.ingestion.acquisition import AcquiredSource
from sourcetrace.ingestion.archive import ExtractionManifest
from sourcetrace.ingestion.scanner import scan_code_sources
from sourcetrace.parsers.javascript_ast import parse_javascript_source
from sourcetrace.parsers.python_ast import parse_acquired_source


def test_js_function_parsing() -> None:
    source = """
function calculateCalories(duration, met) {
    return duration * met * 3.5;
}

async function fetchUserWorkouts(userId) {
    return [];
}
"""
    chunks = parse_javascript_source(
        source=source,
        relative_path="src/utils/calc.js",
        repository_id="repo_123",
        owner_session_id="sess_456",
    )
    assert len(chunks) == 2
    
    c1 = chunks[0]
    assert c1.symbol_name == "calculateCalories"
    assert c1.symbol_type == "function"
    assert c1.language == "javascript"
    assert c1.start_line == 2
    assert c1.end_line == 4
    assert c1.repository_id == "repo_123"
    assert c1.owner_session_id == "sess_456"
    assert "calculateCalories" in c1.content

    c2 = chunks[1]
    assert c2.symbol_name == "fetchUserWorkouts"
    assert c2.symbol_type == "async_function"
    assert c2.language == "javascript"
    assert c2.start_line == 6
    assert c2.end_line == 8


def test_jsx_react_component_and_hook() -> None:
    source = """
import React from 'react';

export const WorkoutCard = ({ name, duration }) => {
    return (
        <div className="workout-card">
            <h3>{name}</h3>
            <p>{duration} mins</p>
        </div>
    );
};

export function useWorkout(id) {
    const [workout, setWorkout] = React.useState(null);
    return workout;
}
"""
    chunks = parse_javascript_source(
        source=source,
        relative_path="src/components/WorkoutCard.jsx",
        repository_id="repo_123",
        owner_session_id="sess_456",
    )
    assert len(chunks) == 2
    
    comp = [c for c in chunks if c.symbol_name == "WorkoutCard"][0]
    assert comp.symbol_type == "react_component"
    assert comp.language == "jsx"
    
    hook = [c for c in chunks if c.symbol_name == "useWorkout"][0]
    assert hook.symbol_type == "hook"
    assert hook.language == "jsx"


def test_ts_interfaces_types_enums_classes() -> None:
    source = """
export interface Workout {
    id: string;
    title: string;
}

export type WorkoutId = string;

export enum WorkoutStatus {
    PLANNED = 'PLANNED',
    COMPLETED = 'COMPLETED'
}

export class WorkoutService {
    async createWorkout(data: Workout): Promise<WorkoutId> {
        return "w_123";
    }
}
"""
    chunks = parse_javascript_source(
        source=source,
        relative_path="src/services/WorkoutService.ts",
        repository_id="repo_123",
        owner_session_id="sess_456",
    )

    names_and_types = {(c.symbol_name, c.symbol_type) for c in chunks}
    assert ("Workout", "interface") in names_and_types
    assert ("WorkoutId", "type_alias") in names_and_types
    assert ("WorkoutStatus", "enum") in names_and_types
    assert ("WorkoutService", "class") in names_and_types
    assert ("WorkoutService.createWorkout", "async_method") in names_and_types


def test_tsx_component() -> None:
    source = """
import React from 'react';

export interface Props {
    title: string;
}

export const Header: React.FC<Props> = ({ title }) => {
    return <header><h1>{title}</h1></header>;
};
"""
    chunks = parse_javascript_source(
        source=source,
        relative_path="src/Header.tsx",
        repository_id="repo_123",
        owner_session_id="sess_456",
    )
    assert len(chunks) == 2
    types = {c.symbol_type for c in chunks}
    assert "interface" in types
    assert "react_component" in types


def test_malformed_js_falls_back_safely() -> None:
    source = "const x = {{{ invalid syntax !!!"
    chunks = parse_javascript_source(
        source=source,
        relative_path="src/broken.js",
        repository_id="repo_123",
        owner_session_id="sess_456",
    )
    assert len(chunks) == 1
    assert chunks[0].symbol_type == "module"
    assert chunks[0].symbol_name == "<module>"


def test_deterministic_chunk_ids() -> None:
    source = "export function run() { return 42; }"
    c1 = parse_javascript_source(source, "run.js", "repo_1", "sess_1")
    c2 = parse_javascript_source(source, "run.js", "repo_1", "sess_1")
    assert len(c1) == 1 and len(c2) == 1
    assert c1[0].chunk_id == c2[0].chunk_id


def test_mixed_python_and_jsts_repository(tmp_path: Path) -> None:
    py_file = tmp_path / "main.py"
    py_file.write_text("def py_func(): pass\n", encoding="utf-8")

    ts_file = tmp_path / "index.ts"
    ts_file.write_text("export function tsFunc() { return 1; }\n", encoding="utf-8")

    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=12345\n", encoding="utf-8")

    node_mod = tmp_path / "node_modules" / "pkg" / "index.js"
    node_mod.parent.mkdir(parents=True)
    node_mod.write_text("console.log('ignored');\n", encoding="utf-8")

    manifest = ExtractionManifest(
        file_count=4,
        total_extracted_bytes=200,
        relative_paths=("main.py", "index.ts", ".env", "node_modules/pkg/index.js"),
    )
    acquired = AcquiredSource(
        extraction_root=tmp_path,
        manifest=manifest,
        source_type="zip",
    )

    scan_res = scan_code_sources(acquired)
    rel_paths = [f.relative_path for f in scan_res.eligible_files]
    assert "main.py" in rel_paths
    assert "index.ts" in rel_paths
    assert ".env" not in rel_paths
    assert "node_modules/pkg/index.js" not in rel_paths

    parse_res = parse_acquired_source(acquired, "repo_mixed", "sess_mixed")
    assert parse_res.parsed_file_count == 2
    symbols = {c.symbol_name for c in parse_res.chunks}
    assert "py_func" in symbols
    assert "tsFunc" in symbols


def test_generic_repository_react_vite() -> None:
    app_tsx = """import React from 'react';

export interface AppProps {
    title: string;
}

export function App({ title }: AppProps) {
    return (
        <div className="app">
            <h1>{title}</h1>
        </div>
    );
}
"""
    use_workout_ts = """import { useState } from 'react';

export interface WorkoutData {
    id: string;
    duration: number;
}

export function useWorkout(id: string) {
    const [data, setData] = useState<WorkoutData | null>(null);
    return { data };
}
"""

    repo_id = "repo_react_vite"
    sess_id = "sess_react_vite"

    chunks_app = parse_javascript_source(app_tsx, "src/App.tsx", repo_id, sess_id)
    chunks_hook = parse_javascript_source(
        use_workout_ts, "src/hooks/useWorkout.ts", repo_id, sess_id
    )

    # 1. App.tsx assertions
    assert len(chunks_app) == 2
    props_chunk = [c for c in chunks_app if c.symbol_name == "AppProps"][0]
    app_chunk = [c for c in chunks_app if c.symbol_name == "App"][0]

    assert props_chunk.repository_id == repo_id
    assert props_chunk.owner_session_id == sess_id
    assert props_chunk.relative_path == "src/App.tsx"
    assert props_chunk.language == "tsx"
    assert props_chunk.symbol_type == "interface"
    assert props_chunk.start_line == 3
    assert props_chunk.end_line == 5

    assert app_chunk.repository_id == repo_id
    assert app_chunk.owner_session_id == sess_id
    assert app_chunk.relative_path == "src/App.tsx"
    assert app_chunk.language == "tsx"
    assert app_chunk.symbol_type == "react_component"
    assert app_chunk.start_line == 7
    assert app_chunk.end_line == 13

    # 2. useWorkout.ts assertions
    assert len(chunks_hook) == 2
    data_chunk = [c for c in chunks_hook if c.symbol_name == "WorkoutData"][0]
    hook_chunk = [c for c in chunks_hook if c.symbol_name == "useWorkout"][0]

    assert data_chunk.relative_path == "src/hooks/useWorkout.ts"
    assert data_chunk.language == "typescript"
    assert data_chunk.symbol_type == "interface"

    assert hook_chunk.relative_path == "src/hooks/useWorkout.ts"
    assert hook_chunk.language == "typescript"
    assert hook_chunk.symbol_type == "hook"
    assert hook_chunk.start_line == 8
    assert hook_chunk.end_line == 11

    # 3. Determinism check
    chunks_app_repeat = parse_javascript_source(app_tsx, "src/App.tsx", repo_id, sess_id)
    assert [c.chunk_id for c in chunks_app] == [c.chunk_id for c in chunks_app_repeat]


def test_generic_repository_node_express() -> None:
    server_js = """const express = require('express');
const app = express();

function startServer(port) {
    app.listen(port);
}

async function connectDatabase(uri) {
    return true;
}

module.exports = { startServer, connectDatabase };
"""
    routes_users_js = """const express = require('express');
const router = express.Router();

function getUsers(req, res) {
    res.json([]);
}

async function createUser(req, res) {
    const user = req.body;
    res.status(201).json(user);
}

module.exports = { getUsers, createUser };
"""

    repo_id = "repo_node_express"
    sess_id = "sess_node_express"

    chunks_server = parse_javascript_source(server_js, "server.js", repo_id, sess_id)
    chunks_routes = parse_javascript_source(routes_users_js, "routes/users.js", repo_id, sess_id)

    # 1. server.js assertions
    start_chunk = [c for c in chunks_server if c.symbol_name == "startServer"][0]
    db_chunk = [c for c in chunks_server if c.symbol_name == "connectDatabase"][0]

    assert start_chunk.repository_id == repo_id
    assert start_chunk.owner_session_id == sess_id
    assert start_chunk.relative_path == "server.js"
    assert start_chunk.language == "javascript"
    assert start_chunk.symbol_type == "function"
    assert start_chunk.start_line == 4
    assert start_chunk.end_line == 6

    assert db_chunk.relative_path == "server.js"
    assert db_chunk.language == "javascript"
    assert db_chunk.symbol_type == "async_function"
    assert db_chunk.start_line == 8
    assert db_chunk.end_line == 10

    # 2. routes/users.js assertions
    get_chunk = [c for c in chunks_routes if c.symbol_name == "getUsers"][0]
    create_chunk = [c for c in chunks_routes if c.symbol_name == "createUser"][0]

    assert get_chunk.repository_id == repo_id
    assert get_chunk.owner_session_id == sess_id
    assert get_chunk.relative_path == "routes/users.js"
    assert get_chunk.language == "javascript"
    assert get_chunk.symbol_type == "function"

    assert create_chunk.relative_path == "routes/users.js"
    assert create_chunk.language == "javascript"
    assert create_chunk.symbol_type == "async_function"

    # 3. Determinism check
    chunks_routes_repeat = parse_javascript_source(
        routes_users_js, "routes/users.js", repo_id, sess_id
    )
    assert [c.chunk_id for c in chunks_routes] == [c.chunk_id for c in chunks_routes_repeat]


def test_generic_repository_typescript_library() -> None:
    types_ts = """export interface ClientConfig {
    endpoint: string;
    timeout: number;
}

export type ResponseData = Record<string, unknown>;

export enum Status {
    IDLE = 'IDLE',
    ACTIVE = 'ACTIVE',
}
"""
    client_ts = """import { ClientConfig, ResponseData } from './types';

export class ApiClient {
    private config: ClientConfig;

    constructor(config: ClientConfig) {
        this.config = config;
    }

    async request(path: string): Promise<ResponseData> {
        return {};
    }

    close(): void {}
}
"""
    index_ts = """export * from './types';
export * from './client';

export function createClient(config: ClientConfig): ApiClient {
    return new ApiClient(config);
}
"""

    repo_id = "repo_ts_lib"
    sess_id = "sess_ts_lib"

    chunks_types = parse_javascript_source(types_ts, "types.ts", repo_id, sess_id)
    chunks_client = parse_javascript_source(client_ts, "client.ts", repo_id, sess_id)
    chunks_index = parse_javascript_source(index_ts, "index.ts", repo_id, sess_id)

    # 1. types.ts assertions
    cfg_chunk = [c for c in chunks_types if c.symbol_name == "ClientConfig"][0]
    resp_chunk = [c for c in chunks_types if c.symbol_name == "ResponseData"][0]
    status_chunk = [c for c in chunks_types if c.symbol_name == "Status"][0]

    assert cfg_chunk.relative_path == "types.ts"
    assert cfg_chunk.language == "typescript"
    assert cfg_chunk.symbol_type == "interface"
    assert cfg_chunk.start_line == 1
    assert cfg_chunk.end_line == 4

    assert resp_chunk.symbol_type == "type_alias"
    assert status_chunk.symbol_type == "enum"

    # 2. client.ts assertions
    cls_chunk = [c for c in chunks_client if c.symbol_name == "ApiClient"][0]
    req_chunk = [c for c in chunks_client if c.symbol_name == "ApiClient.request"][0]
    close_chunk = [c for c in chunks_client if c.symbol_name == "ApiClient.close"][0]

    assert cls_chunk.relative_path == "client.ts"
    assert cls_chunk.language == "typescript"
    assert cls_chunk.symbol_type == "class"

    assert req_chunk.symbol_type == "async_method"
    assert close_chunk.symbol_type == "method"

    # 3. index.ts assertions
    factory_chunk = [c for c in chunks_index if c.symbol_name == "createClient"][0]

    assert factory_chunk.relative_path == "index.ts"
    assert factory_chunk.language == "typescript"
    assert factory_chunk.symbol_type == "function"

    # 4. Determinism check
    chunks_client_repeat = parse_javascript_source(client_ts, "client.ts", repo_id, sess_id)
    assert [c.chunk_id for c in chunks_client] == [c.chunk_id for c in chunks_client_repeat]


def test_simulated_parser_subprocess_crash(monkeypatch) -> None:
    import subprocess

    def mock_run(*args, **kwargs):
        class MockCompletedProcess:
            returncode = 3221225477
            stdout = ""
            stderr = "Access violation"

        return MockCompletedProcess()

    monkeypatch.setattr(subprocess, "run", mock_run)

    source = "const UserRepository = { findById: async (id) => null };"
    chunks = parse_javascript_source(
        source=source,
        relative_path="backend/src/repositories/userRepository.js",
        repository_id="repo_crash_test",
        owner_session_id="sess_crash_test",
    )

    assert len(chunks) == 1
    assert chunks[0].symbol_type == "module"
    assert chunks[0].symbol_name == "<module>"
    assert chunks[0].relative_path == "backend/src/repositories/userRepository.js"
    assert chunks[0].content == source


def test_simulated_parser_subprocess_timeout(monkeypatch) -> None:
    import subprocess

    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="worker", timeout=5.0)

    monkeypatch.setattr(subprocess, "run", mock_run)

    source = "const SlowFile = () => 42;"
    chunks = parse_javascript_source(
        source=source,
        relative_path="src/slow.js",
        repository_id="repo_timeout",
        owner_session_id="sess_timeout",
    )

    assert len(chunks) == 1
    assert chunks[0].symbol_type == "module"
    assert chunks[0].symbol_name == "<module>"


def test_simulated_parser_subprocess_invalid_json(monkeypatch) -> None:
    import subprocess

    def mock_run(*args, **kwargs):
        class MockCompletedProcess:
            returncode = 0
            stdout = "NOT_VALID_JSON"
            stderr = ""

        return MockCompletedProcess()

    monkeypatch.setattr(subprocess, "run", mock_run)

    source = "function hello() {}"
    chunks = parse_javascript_source(
        source=source,
        relative_path="src/invalid_json.js",
        repository_id="repo_invalid_json",
        owner_session_id="sess_invalid_json",
    )

    assert len(chunks) == 1
    assert chunks[0].symbol_type == "module"
    assert chunks[0].symbol_name == "<module>"


def test_no_subprocess_recursion() -> None:
    from sourcetrace.parsers import js_parser_worker

    # Worker must import in-process function directly, not the safe subprocess wrapper
    worker_source = Path(js_parser_worker.__file__).read_text(encoding="utf-8")
    assert "_parse_javascript_source_in_process" in worker_source
    assert "_parse_javascript_source_safe" not in worker_source


def test_fitsync_failing_file_fixture() -> None:
    # Fixture content representing FitSync userRepository pattern
    source = """
const db = require('../config/db');

class UserRepository {
    async findById(id) {
        return db.query('SELECT * FROM users WHERE id = ?', [id]);
    }
}

module.exports = new UserRepository();
"""
    chunks = parse_javascript_source(
        source=source,
        relative_path="backend/src/repositories/userRepository.js",
        repository_id="repo_fitsync_fixture",
        owner_session_id="sess_fitsync_fixture",
    )

    assert len(chunks) >= 1
    # Must return either valid symbols or safe module chunk without process termination
    first_chunk = chunks[0]
    assert first_chunk.repository_id == "repo_fitsync_fixture"
    assert first_chunk.owner_session_id == "sess_fitsync_fixture"
    assert first_chunk.relative_path == "backend/src/repositories/userRepository.js"


