use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use tauri::AppHandle;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

const PROTOCOL_VERSION: u16 = 1;
const ERROR_TAIL_LIMIT: usize = 4_000;
pub const SUPPORTED_METHODS: &[&str] = &[
    "ping", "health", "capabilities", "project_create", "project_open", "project_open_last", "project_save", "timeline_history", "timeline_get", "timeline_command", "timeline_undo", "timeline_redo", "timeline_compile", "revision_list", "revision_generate", "revision_propose", "revision_decide",
    "project_cache_info", "project_cache_clear", "media_import", "media_relink", "proxy_generate", "models_status",
    "model_download", "transcription_generate", "transcript_list", "transcript_edit", "transcript_marker", "analysis_catalog",
    "analysis_run", "analysis_list", "style_recipe_catalog", "style_recipe_propose", "brief_parse", "brief_save", "brief_list", "reference_analyze", "proposal_generate",
    "planning_generate", "planning_list", "planning_review", "proposal_list", "proposal_decide", "export_generate", "export_list", "export_srt", "job_submit", "job_list", "job_cancel", "job_retry",
    "runtime_status", "runtime_summarize", "index_build", "index_status", "index_search",
];

pub fn supports_method(method: &str) -> bool {
    SUPPORTED_METHODS.contains(&method)
}

fn result_has_fields(result: &Value, method: &str) -> bool {
    let required: &[&str] = match method {
        "revision_list" | "revision_generate" | "revision_propose" => &["suggestions", "revision", "warnings"],
        "revision_decide" => &["suggestion", "timeline"],
        "timeline_get" | "timeline_command" | "timeline_undo" | "timeline_redo" | "timeline_compile" => &["timeline"],
        "runtime_status" => &["available", "provider", "models"],
        "runtime_summarize" => &["assetId", "transcriptId", "schemaId", "suggestionOnly", "data", "provenance"],
        "index_status" => &["available", "stale"],
        "index_search" => &["results", "index", "stale"],
        "planning_review" => &["plan"],
        "health" => &["service", "ffmpeg", "storage"],
        "capabilities" => &["schemaVersion", "platform", "cpu", "memory", "mediaTools", "disk", "recommendations"],
        "project_create" => &["projectId", "path", "name"],
        "project_open" | "project_open_last" => &["projectId", "path", "state"],
        "project_save" => &["projectId", "revision"],
        "timeline_history" => &["snapshots", "truncated"],
        "project_cache_info" => &["projectId", "projectPath", "entries", "artifactCount", "totalBytes", "activeJobs", "canClear", "confirmationToken", "protectedPaths"],
        "project_cache_clear" => &["clearedCount", "clearedBytes", "snapshot"],
        "media_import" => &["imported", "errors", "candidateCount"],
        "models_status" => &["models", "recommended", "backendAvailable", "backend", "detail"],
        "transcript_list" => &["transcripts"],
        "transcript_edit" | "transcript_marker" => &["id", "assetId", "segments", "words", "markers"],
        "analysis_catalog" => &["detectors"],
        "analysis_list" => &["results", "warnings", "sourceStatus", "prerequisites"],
        "style_recipe_catalog" => &["recipes"],
        "style_recipe_propose" => &["recipeId", "recipeVersion", "traits", "requiresReview", "explanation", "generatorId", "generatorVersion"],
        "brief_parse" => &["constraints", "parserId", "parserVersion"],
        "brief_save" => &["brief"],
        "brief_list" => &["briefs"],
        "reference_analyze" => &["generatorId", "generatorVersion", "traits"],
        "planning_list" | "proposal_list" => &["plans"],
        "export_list" => &["exports"],
        "export_srt" => &["relativePath", "captionCount"],
        "job_list" => &["jobs"],
        "proxy_generate" | "transcription_generate" | "analysis_run" | "planning_generate" | "proposal_generate" | "export_generate" | "job_submit" | "job_cancel" | "job_retry" => &["id", "state"],
        "model_download" if result.get("deduplicated") == Some(&Value::Bool(true)) => &["state", "deduplicated"],
        "index_build" => &["id", "state"],
        "model_download" => &["id", "state"],
        _ => &[],
    };
    let Some(object) = result.as_object() else { return false };
    if !required.iter().all(|field| object.contains_key(*field)) { return false; }
    let arrays: &[&str] = match method {
        "revision_list" | "revision_generate" | "revision_propose" => &["suggestions", "warnings"],
        "runtime_status" => &["models"],
        "index_search" => &["results"],
        "timeline_history" => &["snapshots"],
        "project_cache_info" => &["entries", "activeJobs", "protectedPaths"],
        "media_import" => &["imported", "errors"],
        "models_status" => &["models"],
        "transcript_list" => &["transcripts"],
        "transcript_edit" | "transcript_marker" => &["segments", "words", "markers"],
        "analysis_catalog" => &["detectors"],
        "analysis_list" => &["results", "warnings"],
        "style_recipe_catalog" => &["recipes"],
        "brief_list" => &["briefs"],
        "planning_list" | "proposal_list" => &["plans"],
        "export_list" => &["exports"],
        "job_list" => &["jobs"],
        _ => &[],
    };
    let objects: &[&str] = match method {
        "revision_decide" => &["suggestion", "timeline"],
        "timeline_get" | "timeline_command" | "timeline_undo" | "timeline_redo" | "timeline_compile" => &["timeline"],
        "planning_review" => &["plan"],
        "runtime_summarize" => &["data", "provenance"],
        "index_search" => &["index"],
        "health" => &["service", "ffmpeg", "storage"],
        "capabilities" => &["platform", "cpu", "memory", "mediaTools", "disk", "recommendations"],
        "project_open" | "project_open_last" => &["state"],
        "project_cache_clear" => &["snapshot"],
        "transcript_edit" | "transcript_marker" => &[],
        "brief_parse" => &["constraints"],
        "brief_save" => &["brief"],
        "reference_analyze" | "style_recipe_propose" => &["traits"],
        _ => &[],
    };
    if arrays.iter().any(|field| !object[*field].is_array()) || objects.iter().any(|field| !object[*field].is_object()) { return false; }
    match method {
        "revision_list" | "revision_generate" | "revision_propose" => object["revision"].as_u64().is_some(),
        "runtime_status" => object["available"].is_boolean() && object["provider"].is_string(),
        "runtime_summarize" => object["suggestionOnly"] == Value::Bool(true),
        "index_status" => object["available"].is_boolean() && object["stale"].is_boolean(),
        "index_search" => object["stale"].is_boolean(),
        "timeline_history" => object["truncated"].is_boolean(),
        "project_create" => object["projectId"].is_string() && object["path"].is_string() && object["name"].is_string(),
        "project_open" | "project_open_last" => object["projectId"].is_string() && object["path"].is_string(),
        "project_save" => object["projectId"].is_string() && object["revision"].is_number(),
        "project_cache_info" => object["artifactCount"].is_number() && object["totalBytes"].is_number() && object["canClear"].is_boolean(),
        "media_import" => object["candidateCount"].is_number(),
        "models_status" => object["backendAvailable"].is_boolean(),
        "style_recipe_propose" => object["requiresReview"] == Value::Bool(true) && object["recipeId"].is_string() && object["recipeVersion"].is_string(),
        "brief_parse" => object["parserId"].is_string() && object["parserVersion"].is_string(),
        "export_srt" => object["relativePath"].is_string() && object["captionCount"].is_number(),
        "model_download" if object.get("deduplicated") == Some(&Value::Bool(true)) => object["state"].as_str() == Some("completed"),
        "index_build" | "model_download" | "proxy_generate" | "transcription_generate" | "analysis_run" | "planning_generate" | "proposal_generate" | "export_generate" | "job_submit" | "job_cancel" | "job_retry" => object["id"].is_string() && object["state"].is_string(),
        _ => true,
    }
}

fn decode_response(method: &str, line: &str, expected_id: u64) -> Result<Value, String> {
    let raw: Value = serde_json::from_str(line).map_err(|error| format!("Invalid service response: {error}"))?;
    let envelope = raw.as_object().ok_or_else(|| "Invalid service response: envelope must be a JSON object".to_string())?;
    let succeeded = envelope.get("ok").and_then(Value::as_bool).ok_or_else(|| "Invalid service response: envelope omitted a boolean status".to_string())?;
    if succeeded {
        if !envelope.get("result").is_some_and(Value::is_object) {
            return Err("Invalid service response: successful response must contain an object result".into());
        }
        if envelope.contains_key("error") {
            return Err("Invalid service response: success included an error".into());
        }
    } else {
        if envelope.contains_key("result") {
            return Err("Invalid service response: failed response included a result".into());
        }
        if !envelope.get("error").is_some_and(Value::is_object) {
            return Err("Invalid service response: failed response must contain an error object".into());
        }
    }
    let response: Response = serde_json::from_value(raw).map_err(|error| format!("Invalid service response: {error}"))?;
    if response.protocol_version != PROTOCOL_VERSION || response.request_id != expected_id {
        return Err("Service protocol mismatch".into());
    }
    if response.ok {
        if response.error.is_some() {
            return Err("Invalid service response: success included an error".into());
        }
        let result = response.result.ok_or_else(|| "Invalid service response: successful response omitted its result".to_string())?;
        if !result_has_fields(&result, method) {
            return Err(format!("Invalid service response: result for {method} is missing required fields or has the wrong type"));
        }
        Ok(result)
    } else {
        if response.result.is_some() {
            return Err("Invalid service response: failed response included a result".into());
        }
        let error = response.error.ok_or_else(|| "Service request failed".to_string())?;
        Err(format!("{}: {}", error.code, error.message))
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Request<'a> {
    protocol_version: u16,
    request_id: u64,
    method: &'a str,
    params: Value,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Response {
    protocol_version: u16,
    request_id: u64,
    ok: bool,
    #[serde(default)]
    result: Option<Value>,
    #[serde(default)]
    error: Option<ServiceError>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ServiceError {
    code: String,
    message: String,
}

enum SidecarEvent {
    Stdout(Vec<u8>),
    Stderr(Vec<u8>),
    Error(String),
    Terminated,
}

enum RunningService {
    Python {
        child: Child,
        input: BufWriter<ChildStdin>,
        output: BufReader<ChildStdout>,
    },
    Sidecar {
        child: CommandChild,
        events: mpsc::Receiver<SidecarEvent>,
        pending: Vec<u8>,
        stderr_tail: Vec<u8>,
        terminated: Arc<AtomicBool>,
    },
}

#[derive(Default)]
pub struct ServiceClient {
    process: Option<RunningService>,
    next_id: u64,
}

impl ServiceClient {
    fn running(&mut self) -> bool {
        match self.process.as_mut() {
            Some(RunningService::Python { child, .. }) => child.try_wait().ok().flatten().is_none(),
            Some(RunningService::Sidecar { terminated, .. }) => !terminated.load(Ordering::Acquire),
            None => false,
        }
    }

    fn start(&mut self, app: &AppHandle) -> Result<(), String> {
        if self.running() {
            return Ok(());
        }
        self.stop();

        let python_override = std::env::var_os("LOCAL_CUT_PYTHON");
        let service_override = std::env::var_os("LOCAL_CUT_SERVICE_DIR");
        if cfg!(debug_assertions) || python_override.is_some() || service_override.is_some() {
            let root = service_override.unwrap_or_else(|| concat!(env!("CARGO_MANIFEST_DIR"), "/../service").into());
            let mut command = Command::new(python_override.unwrap_or_else(|| "python".into()));
            command.args(["-m", "localcut_service"]).current_dir(root).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
            let mut child = command.spawn().map_err(|error| format!("Unable to start local Python service: {error}"))?;
            let input = child.stdin.take().ok_or_else(|| "Python service stdin unavailable".to_string())?;
            let output = child.stdout.take().ok_or_else(|| "Python service stdout unavailable".to_string())?;
            self.process = Some(RunningService::Python { child, input: BufWriter::new(input), output: BufReader::new(output) });
            return Ok(());
        }

        let command = app.shell().sidecar("localcut-service").map_err(|error| format!("Packaged local service is unavailable: {error}"))?.set_process_group(true);
        let (mut receiver, child) = command.spawn().map_err(|error| format!("Unable to start packaged local service: {error}"))?;
        let (sender, events) = mpsc::channel();
        let terminated = Arc::new(AtomicBool::new(false));
        let thread_terminated = Arc::clone(&terminated);
        tauri::async_runtime::spawn(async move {
            while let Some(event) = receiver.recv().await {
                let mapped = match event {
                    CommandEvent::Stdout(bytes) => SidecarEvent::Stdout(bytes),
                    CommandEvent::Stderr(bytes) => SidecarEvent::Stderr(bytes),
                    CommandEvent::Error(message) => SidecarEvent::Error(message),
                    CommandEvent::Terminated(_) => {
                        thread_terminated.store(true, Ordering::Release);
                        let _ = sender.send(SidecarEvent::Terminated);
                        return;
                    }
                    _ => continue,
                };
                if sender.send(mapped).is_err() {
                    break;
                }
            }
            thread_terminated.store(true, Ordering::Release);
            let _ = sender.send(SidecarEvent::Terminated);
        });
        self.process = Some(RunningService::Sidecar { child, events, pending: Vec::new(), stderr_tail: Vec::new(), terminated });
        Ok(())
    }

    fn stop(&mut self) {
        match self.process.take() {
            Some(RunningService::Python { mut child, input, output }) => {
                drop(input);
                drop(output);
                let _ = child.kill();
                let _ = child.wait();
            }
            Some(RunningService::Sidecar { child, .. }) => {
                let _ = child.kill();
            }
            None => {}
        }
    }

    fn write_request(&mut self, bytes: &[u8]) -> Result<(), String> {
        match self.process.as_mut() {
            Some(RunningService::Python { input, .. }) => input.write_all(bytes).and_then(|_| input.flush()).map_err(|error| error.to_string()),
            Some(RunningService::Sidecar { child, .. }) => child.write(bytes).map_err(|error| error.to_string()),
            None => Err("Local service is unavailable".into()),
        }
    }

    fn read_response_line(&mut self) -> Result<String, String> {
        match self.process.as_mut() {
            Some(RunningService::Python { output, .. }) => {
                let mut line = String::new();
                let count = output.read_line(&mut line).map_err(|error| error.to_string())?;
                if count == 0 { Err("Local service closed its response stream".into()) } else { Ok(line) }
            }
            Some(RunningService::Sidecar { events, pending, stderr_tail, .. }) => loop {
                if let Some(end) = pending.iter().position(|byte| *byte == b'\n') {
                    let line: Vec<u8> = pending.drain(..=end).collect();
                    return String::from_utf8(line).map_err(|error| format!("Local service returned invalid UTF-8: {error}"));
                }
                match events.recv().map_err(|_| "Packaged local service output ended unexpectedly".to_string())? {
                    SidecarEvent::Stdout(bytes) => pending.extend(bytes),
                    SidecarEvent::Stderr(bytes) => {
                        stderr_tail.extend(bytes);
                        if stderr_tail.len() > ERROR_TAIL_LIMIT {
                            let excess = stderr_tail.len() - ERROR_TAIL_LIMIT;
                            stderr_tail.drain(..excess);
                        }
                    }
                    SidecarEvent::Error(error) => return Err(format!("Packaged local service error: {error}")),
                    SidecarEvent::Terminated => {
                        let detail = String::from_utf8_lossy(stderr_tail).trim().to_string();
                        return Err(if detail.is_empty() { "Packaged local service exited before responding".into() } else { format!("Packaged local service exited before responding: {detail}") });
                    }
                }
            },
            None => Err("Local service is unavailable".into()),
        }
    }

    pub fn request(&mut self, app: &AppHandle, method: &str, params: Value) -> Result<Value, String> {
        if !supports_method(method) {
            return Err("Unsupported service method".into());
        }
        self.start(app)?;
        self.next_id += 1;
        let request_id = self.next_id;
        let request = Request { protocol_version: PROTOCOL_VERSION, request_id, method, params };
        let mut encoded = serde_json::to_vec(&request).map_err(|error| error.to_string())?;
        encoded.push(b'\n');
        self.write_request(&encoded)?;
        let line = self.read_response_line()?;
        decode_response(method, &line, request_id)
    }
}

impl Drop for ServiceClient {
    fn drop(&mut self) { self.stop(); }
}

#[cfg(test)]
mod tests {
    use super::{decode_response, result_has_fields, supports_method};
    use serde_json::json;

    #[test]
    fn method_allowlist_includes_service_methods_used_by_the_editor() {
        assert!(supports_method("timeline_history"));
        assert!(supports_method("media_relink"));
        assert!(supports_method("project_open"));
        assert!(supports_method("style_recipe_catalog"));
        assert!(supports_method("style_recipe_propose"));
        assert!(!supports_method("arbitrary_method"));
    }

    #[test]
    fn validates_project_open_and_reviewable_recipe_responses() {
        assert!(result_has_fields(&json!({"projectId":"p","path":"project","state":{}}), "project_open"));
        assert!(!result_has_fields(&json!({"projectId":"p","path":"project","state":[]}), "project_open"));
        assert!(result_has_fields(&json!({"recipes":[]}), "style_recipe_catalog"));
        assert!(!result_has_fields(&json!({"recipes":{}}), "style_recipe_catalog"));
        let mut proposal = json!({"recipeId":"fast-educational","recipeVersion":"1","traits":{},"requiresReview":true,"explanation":"Review neutral traits","generatorId":"local","generatorVersion":"1"});
        assert!(result_has_fields(&proposal, "style_recipe_propose"));
        proposal["requiresReview"] = json!(false);
        assert!(!result_has_fields(&proposal, "style_recipe_propose"));
    }

    #[test]
    fn validates_revision_response_envelopes() {
        for method in ["revision_list", "revision_generate", "revision_propose"] {
            assert!(supports_method(method));
            assert!(result_has_fields(&json!({"suggestions":[],"revision":2,"warnings":[]}), method));
            assert!(!result_has_fields(&json!({"suggestions":{},"revision":2,"warnings":[]}), method));
            assert!(!result_has_fields(&json!({"revision":2}), method));
            assert!(!result_has_fields(&json!({"suggestions":[],"revision":"two","warnings":[]}), method));
        }
        assert!(supports_method("revision_decide"));
        assert!(result_has_fields(&json!({"suggestion":{},"timeline":{}}), "revision_decide"));
        assert!(!result_has_fields(&json!({"suggestion":{},"timeline":[]}), "revision_decide"));
    }

    #[test]
    fn validates_timeline_response_envelopes() {
        for method in ["timeline_get", "timeline_command", "timeline_undo", "timeline_redo", "timeline_compile"] {
            assert!(supports_method(method));
            assert!(result_has_fields(&json!({"timeline":{}}), method));
            assert!(!result_has_fields(&json!({"timeline":[]}), method));
            assert!(!result_has_fields(&json!({"revision":2}), method));
            assert!(!result_has_fields(&json!({"suggestions":[],"revision":"two","warnings":[]}), method));
        }
    }

    #[test]
    fn validates_written_plan_responses() {
        assert!(supports_method("planning_generate"));
        assert!(supports_method("planning_list"));
        assert!(supports_method("planning_review"));
        assert!(result_has_fields(&json!({"plans":[]}), "planning_list"));
        assert!(!result_has_fields(&json!({"plans":{}}), "planning_list"));
        assert!(result_has_fields(&json!({"plan":{}}), "planning_review"));
        assert!(!result_has_fields(&json!({"plan":[]}), "planning_review"));
        assert!(result_has_fields(&json!({"id":"job","state":"queued"}), "planning_generate"));
        assert!(!result_has_fields(&json!({"id":1,"state":"queued"}), "planning_generate"));
    }

    #[test]
    fn validates_runtime_and_index_response_shapes() {
        assert!(supports_method("index_build"));
        assert!(supports_method("runtime_status"));
        assert!(result_has_fields(&json!({"available":false,"provider":"ollama","models":[]}), "runtime_status"));
        assert!(!result_has_fields(&json!({"available":false,"provider":"ollama","models":{}}), "runtime_status"));
        assert!(result_has_fields(&json!({"available":false,"stale":false}), "index_status"));
        assert!(!result_has_fields(&json!({"available":false,"stale":"false"}), "index_status"));
        assert!(result_has_fields(&json!({"results":[],"index":{},"stale":false}), "index_search"));
        assert!(!result_has_fields(&json!({"results":[],"index":[],"stale":false}), "index_search"));
    }

    #[test]
    fn rejects_malformed_or_mismatched_success_responses() {
        assert_eq!(
            decode_response("health", r#"{"protocolVersion":1,"requestId":7,"ok":true,"result":{"service":{},"ffmpeg":{},"storage":{}}}"#, 7).unwrap(),
            json!({"service":{},"ffmpeg":{},"storage":{}})
        );
        assert!(decode_response("health", r#"{"protocolVersion":1,"requestId":8,"ok":true,"result":{"service":{},"ffmpeg":{},"storage":{}}}"#, 7).unwrap_err().contains("protocol mismatch"));
        assert!(decode_response("health", r#"{"protocolVersion":1,"requestId":7,"ok":true,"result":{"service":{}}}"#, 7).unwrap_err().contains("required fields"));
        assert!(decode_response("health", r#"{"protocolVersion":1,"requestId":7,"ok":true,"result":[]}"#, 7).unwrap_err().contains("object result"));
        assert!(decode_response("health", r#"{"protocolVersion":1,"requestId":7,"ok":true,"result":{"service":{},"ffmpeg":{},"storage":{}},"error":{"code":"bad","message":"bad"}}"#, 7).unwrap_err().contains("included an error"));
        assert!(decode_response("health", r#"{"protocolVersion":1,"requestId":7,"ok":true,"result":null}"#, 7).unwrap_err().contains("object result"));
    }

    #[test]
    fn validates_service_failure_envelopes_and_required_result_types() {
        assert!(decode_response("health", r#"{"protocolVersion":1,"requestId":7,"ok":false,"error":{"code":"invalid_request","message":"bad path"}}"#, 7).unwrap_err().contains("invalid_request: bad path"));
        assert!(decode_response("health", r#"{"protocolVersion":1,"requestId":7,"ok":false}"#, 7).unwrap_err().contains("error object"));
        assert!(decode_response("health", r#"{"protocolVersion":1,"requestId":7,"ok":false,"result":null,"error":{"code":"invalid_request","message":"bad path"}}"#, 7).unwrap_err().contains("included a result"));
        assert!(!result_has_fields(&json!({"models":"wrong","recommended":"tiny","backendAvailable":true,"backend":"whisper","detail":"ok"}), "models_status"));
    }
}
