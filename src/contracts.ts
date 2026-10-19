export const PROTOCOL_VERSION = 1 as const;
export const SERVICE_METHODS = [
  "ping", "health", "capabilities", "project_create", "project_open", "project_open_last", "project_save", "timeline_history", "timeline_get", "timeline_command", "timeline_undo", "timeline_redo", "timeline_compile", "revision_list", "revision_generate", "revision_propose", "revision_decide",
  "project_cache_info", "project_cache_clear", "media_import", "media_relink", "proxy_generate", "models_status",
  "model_download", "transcription_generate", "transcript_list", "transcript_edit", "transcript_marker", "analysis_catalog",
  "analysis_run", "analysis_list", "brief_parse", "brief_save", "brief_list", "style_recipe_catalog", "style_recipe_propose", "reference_analyze", "proposal_generate",
  "planning_generate", "planning_list", "planning_review", "proposal_list", "proposal_decide", "export_generate", "export_list", "export_srt", "job_submit", "job_list", "job_cancel", "job_retry",
  "runtime_status", "runtime_summarize", "index_build", "index_status", "index_search",
] as const;
export type ServiceMethod = typeof SERVICE_METHODS[number];

export interface ServiceContractMap {
  runtime_status: { params: undefined; result: RuntimeStatus };
  runtime_summarize: { params: { path: string; assetId: string }; result: { assetId: string; transcriptId: string; schemaId: string; suggestionOnly: true; data: { summary: string; topics: string[] }; provenance: ModelExecution } };
  index_build: { params: { path: string; semantic?: boolean; force?: boolean }; result: JobSubmissionResponse };
  index_status: { params: { path: string }; result: FootageIndexStatus };
  index_search: { params: { path: string; query: string; mode?: "exact" | "semantic"; filters?: FootageFilters; limit?: number }; result: { results: FootageMatch[]; index: FootageIndexStatus; stale: boolean } };
  ping: { params: undefined; result: { service: string; version: string; protocolVersion: number } };
  health: { params: undefined; result: ServiceHealth };
  capabilities: { params: undefined; result: CapabilityProfile };
  project_create: { params: { path: string; name: string; settings?: Record<string, unknown> }; result: { projectId: string; path: string; name: string } };
  project_open_last: { params: undefined; result: LastProjectResponse };
  project_open: { params: { path: string }; result: LastProjectResponse };
  project_save: { params: { path: string; state: PersistedProjectState }; result: { projectId: string; revision: number } };
  revision_list: { params: { path: string }; result: RevisionListing };
  revision_generate: { params: { path: string }; result: RevisionListing };
  revision_propose: { params: { path: string; text: string; selectedSuggestionId?: string; selectedClipId?: string }; result: RevisionListing };
  revision_decide: { params: { path: string; suggestionId: string; decision: "accepted" | "rejected" | "undone"; expectedRevision: number }; result: { suggestion: RevisionSuggestion; timeline: TimelineDocument } };
  timeline_get: { params: { path: string }; result: { timeline: TimelineDocument } };
  timeline_command: { params: { path: string; expectedRevision: number; command: TimelineCommand }; result: { timeline: TimelineDocument } };
  timeline_undo: { params: { path: string; expectedRevision: number }; result: { timeline: TimelineDocument } };
  timeline_redo: { params: { path: string; expectedRevision: number }; result: { timeline: TimelineDocument } };
  timeline_compile: { params: { path: string; planId: string; sectionIds: string[]; expectedReviewVersion: number; expectedRevision: number }; result: { timeline: TimelineDocument } };
  timeline_history: { params: { path: string }; result: Record<string, unknown> };
  project_cache_info: { params: { path: string }; result: ProjectCacheSnapshot };
  project_cache_clear: { params: { path: string; confirmationToken: string }; result: Record<string, unknown> };
  media_import: { params: { path: string; inputs: string[]; storageMode?: "linked" | "managed" }; result: Record<string, unknown> };
  media_relink: { params: { path: string; assetId: string; candidatePath: string; allowChanged?: boolean }; result: Record<string, unknown> };
  proxy_generate: { params: { path: string; assetId: string }; result: JobSubmissionResponse };
  models_status: { params: { path: string }; result: ModelsResponse };
  model_download: { params: { path: string; model: string }; result: JobRecord | { state: "completed"; deduplicated: true } };
  transcription_generate: { params: { path: string; assetId: string; model: string }; result: JobSubmissionResponse };
  transcript_list: { params: { path: string; assetId: string }; result: TranscriptResponse };
  transcript_edit: { params: { path: string; transcriptId: string; segmentId: string; text: string | null }; result: Transcript };
  transcript_marker: { params: { path: string; transcriptId: string; sourceInTicks?: number; sourceOutTicks?: number; marker?: "required" | "excluded" | "uncertain"; markerId?: string; delete?: boolean }; result: Transcript };
  analysis_catalog: { params: undefined; result: { detectors: AnalysisDetector[] } };
  analysis_run: { params: { path: string; assetId: string; detector: string; settings?: Record<string, number> }; result: JobSubmissionResponse };
  analysis_list: { params: { path: string; assetId?: string; detectorId?: string; verifyIntegrity?: boolean }; result: { results: AnalysisResult[]; warnings?: AnalysisWarning[]; sourceStatus?: AnalysisResult["sourceStatus"]; prerequisites?: Record<string, unknown> } };
  brief_parse: { params: { briefText: string; overrides?: Record<string, unknown> }; result: { constraints: CreativeBrief["constraints"]; parserId: string; parserVersion: string } };
  brief_save: { params: { path: string; briefText: string; constraints?: CreativeBrief["constraints"]; traits?: Record<string, unknown>; traitOrigin?: "user" | "reference-derived"; styleReviewed?: boolean }; result: { brief: CreativeBrief } };
  brief_list: { params: { path: string }; result: { briefs: CreativeBrief[] } };
  style_recipe_catalog: { params: undefined; result: { recipes: StyleRecipe[] } };
  style_recipe_propose: { params: { recipeId: string; inspiration?: string }; result: StyleRecipeProposal };
  reference_analyze: { params: { referencePath: string }; result: ReferenceTraitProfile };
  planning_generate: { params: { path: string; briefId: string; assetId: string; query?: string; searchMode?: "exact" | "semantic" }; result: JobSubmissionResponse };
  planning_list: { params: { path: string; briefId?: string }; result: { plans: WrittenPlanRecord[] } };
  planning_review: { params: { path: string; planId: string; action: "approve" | "reject" | "replace" | "reorder" | "lock" | "unlock"; sectionId?: string; candidateId?: string; order?: string[]; expectedReviewVersion: number }; result: { plan: WrittenPlanRecord } };
  proposal_generate: { params: { path: string; assetId: string; briefId: string; transcriptId: string }; result: JobSubmissionResponse };
  proposal_list: { params: { path: string; briefId?: string }; result: { plans: EditProposal[] } };
  proposal_decide: { params: { path: string; planId: string; suggestionId: string; decision: "accepted" | "rejected" | "pending"; expectedRevision: number; createCaptions?: boolean }; result: Record<string, unknown> };
  export_generate: { params: { path: string; burnCaptions?: boolean }; result: JobSubmissionResponse };
  export_list: { params: { path: string }; result: { exports: ExportRecord[] } };
  export_srt: { params: { path: string }; result: { relativePath: string; captionCount: number } };
  job_submit: { params: { job: JobInput }; result: JobSubmissionResponse };
  job_list: { params: { projectId?: string; limit?: number }; result: { jobs: JobRecord[] } };
  job_cancel: { params: { jobId: string }; result: JobCommandResponse };
  job_retry: { params: { jobId: string }; result: JobCommandResponse };
}
type Assert<T extends true> = T;
type ContractMethodsAreComplete = Assert<Exclude<ServiceMethod, keyof ServiceContractMap> extends never ? true : false>;
type ContractMethodsAreCurrent = Assert<Exclude<keyof ServiceContractMap, ServiceMethod> extends never ? true : false>;
export type ServiceContractCheck = ContractMethodsAreComplete | ContractMethodsAreCurrent;
export const SERVICE_CONTRACT_MAP_ALIGNED: ServiceContractCheck = true;
export type ServiceParams<M extends ServiceMethod> = ServiceContractMap[M]["params"];
export type ServiceResult<M extends ServiceMethod> = ServiceContractMap[M]["result"];

export type JobKind = "proxy" | "transcription" | "analysis" | "suggestion" | "model_download" | "indexing" | "rendering" | "planning";
export type JobState = "queued" | "running" | "completed" | "failed" | "retrying" | "cancelled";
export interface JobSubmissionResponse { id: string; state: JobState; deduplicated?: boolean; output?: Record<string, unknown> | null; }
export interface JobCommandResponse { id: string; state: JobState; cancelRequested?: boolean; }
export interface JobInput { projectPath: string; kind: JobKind; payload: Record<string, unknown>; sourceFingerprint: string; assetId?: string; modelId?: string; modelVersion?: string; generatorId?: string; generatorVersion?: string; config?: Record<string, unknown>; dependencies?: string[]; maxAttempts?: number; pipelineVersion?: string; }
export interface JobRecord { id: string; projectId: string; kind: JobKind; state: JobState; progress: number; stage: string; message?: string | null; attempts: number; maxAttempts: number; cancelRequested: boolean; dependencies: string[]; payload?: Record<string, unknown>; output: Record<string, unknown> | null; error: Record<string, unknown> | null; createdAt: string; }

export interface CapabilityProfile {
  schemaVersion: number;
  collectedAt: string;
  platform: { system: string; release: string; machine: string; pythonBits: string };
  cpu: { logicalCores: number | null; brand: string | null; flags: string[]; source: string; confidence: string };
  memory: { totalBytes: number | null; availableBytes: number | null; source: string; confidence: string };
  gpus: Array<{ name: string; vendor: string; dedicatedVramBytes: number | null; source: string; confidence: string }>;
  mediaTools: { ffmpeg: Record<string, unknown>; ffprobe: Record<string, unknown>; encoderListingSemantics: string };
  disk: { totalBytes: number | null; freeBytes: number | null; source: string; confidence: string };
  recommendations: { modelSizeClass: string; proxyPreset: string; confidence: string };
}

export interface ServiceRequest<P extends Record<string, unknown> = Record<string, never>> {
  protocolVersion: typeof PROTOCOL_VERSION;
  requestId: number;
  method: ServiceMethod;
  params: P;
}

export interface ServiceError {
  code: string;
  message: string;
}

export type ServiceResponse<T = Record<string, unknown>> =
  | { protocolVersion: typeof PROTOCOL_VERSION; requestId: number; ok: true; result: T }
  | { protocolVersion: typeof PROTOCOL_VERSION; requestId: number; ok: false; error: ServiceError };

export interface HealthCheck {
  available: boolean;
  detail: string;
  version?: string;
  path?: string;
}

export interface ServiceHealth {
  service: HealthCheck;
  ffmpeg: HealthCheck;
  storage: HealthCheck;
}

export interface PersistedProjectState {
  projectId: string;
  revision: number;
  settings: Record<string, unknown>;
  assets: Array<Record<string, unknown>>;
  timelineItems: Array<{
    id: string;
    assetId: string;
    itemType: "clip" | "caption" | "audio" | "overlay";
    trackId: string;
    timelineStartTicks: number;
    timelineDurationTicks: number;
    sourceInTicks: number;
    sourceOutTicks: number;
    properties?: Record<string, unknown>;
  }>;
  captions?: Array<{ id: string; timelineItemId: string; text: string; language: string; style: Record<string, unknown> }>;
  transcripts: Array<Record<string, unknown>>;
  analyses: Array<Record<string, unknown>>;
}

export interface LastProjectResponse {
  projectId: string;
  path: string;
  state: PersistedProjectState;
  recovery?: { checkpointRevision: number; checkpointUpdatedAt: string; recoveredAt: string; message: string } | null;
}
export interface ProjectCacheEntry { artifactId: string; kind: string; relativePath: string; path: string; status: string; sizeBytes: number; }
export interface ProjectCacheSnapshot { projectId: string; projectPath: string; entries: ProjectCacheEntry[]; artifactCount: number; totalBytes: number; activeJobs: Array<{ id: string; kind: string; state: string }>; canClear: boolean; confirmationToken: string | null; protectedPaths: string[]; }
export interface TranscriptRange { sourceInTicks: number; sourceOutTicks: number; }
export interface TranscriptSegment extends TranscriptRange { id: string; text: string; originalText: string; editedText: string | null; confidence: number | null; speakerOrdinal: number | null; }
export interface TranscriptWord extends TranscriptSegment { segmentId: string | null; ordinal: number; timingUncertain?: boolean; }
export interface Transcript { id: string; assetId: string; language: string; status?: "ready" | "stale" | string; modelId: string; modelVersion: string; generatorId: string; generatorVersion: string; segments: TranscriptSegment[]; words: TranscriptWord[]; markers: Array<TranscriptRange & {id: string; marker: string}>; }
export interface WaveformPreview { sourceStartTicks: number; durationTicks: number; sampleCount: number; originalBinCount: number; peaks: Array<[number, number]>; }
export interface TranscriptResponse { transcripts: Transcript[]; playbackUrl?: string; sourceMinusProxySeconds?: number; waveform?: WaveformPreview | null; }
export interface AnalysisDetector { id: string; name: string; version: string; settings: Record<string, [number, number, number]>; needsAudio?: boolean; needsVideo?: boolean; needsTranscript?: boolean; }
export interface AnalysisWarning { detectorId: string; artifactId: string; code: string; message: string; }
export interface AnalysisResult { id: string; assetId: string; detectorId: string; detectorVersion: string; sourceInTicks: number; sourceOutTicks: number; confidence: number | null; detectorName: string; artifactId: string | null; sourceFingerprint: string; sourceStatus?: "metadata_matches_import" | "changed_since_import" | "unavailable" | "missing_asset" | "unknown"; validity?: "integrity_ok" | "stale" | "invalid" | "unknown"; validityReason?: string; config: Record<string, unknown>; createdAt: string; thumbnailUrl?: string; result: { explanation: string; detectorName: string; detectorVersion: string; signalStrength: number | null; suggestionOnly: true; thumbnailPath?: string; matchedSourceInTicks?: number; matchedSourceOutTicks?: number; sampleIsPoint?: boolean; [key: string]: unknown }; }
export interface CreativeBrief { id: string; revision: number; briefText: string; constraints: { audience: string; objective: string; tone: string; targetDurationMinutes: number | null; aspectRatio: string; mustKeep: string[]; mustRemove: string[]; preferredOrder: string[]; schemaVersion?: 2; platform?: string; desiredOpening?: string; desiredEnding?: string; recipeId?: string; recipeVersion?: string; styleTraits?: ReferenceTraitProfile["traits"]; styleReviewed?: boolean }; traits?: Record<string, { value: unknown; origin: string; generatorId: string; generatorVersion: string }>; createdAt: string; }
export type NarrativeStructureTrait = "unknown" | "chronological" | "problem-solution" | "question-answer" | "step-by-step" | "demonstration-first" | "hook-explanation-summary" | "story-led-lesson" | "before-after";
export interface ReferenceTraitProfile { generatorId: string; generatorVersion: string; analyzedSeconds: number; sampleFrames: number; explanation: string; ephemeral: boolean; traits: { pacing: "slow" | "moderate" | "fast" | "unknown"; averageShotLengthSeconds: number | null; jumpCutFrequencyPerMinute: number | null; captionDensityPerMinute: number | null; punchInFrequencyPerMinute: number | null; musicEnergy: "low" | "moderate" | "high" | "unknown"; humorLevel: "low" | "moderate" | "high" | "unknown"; narrativeStructure: NarrativeStructureTrait; }; }
export interface ProposalOperation { id: string; type: "include_source_range" | "exclude_source_range" | "punch_in" | "reorder_passages"; assetId: string; sourceInTicks: number; sourceOutTicks: number; text?: string; status: "pending" | "accepted" | "rejected"; confidence: number | null; protected?: boolean; timingUncertain?: boolean; rationale: string; [key: string]: unknown; }
export interface ProposalStyleGuidance { trait: "pacing"; value: "slow" | "moderate" | "fast"; origin: string; generatorId: string; generatorVersion: string; gapThresholdSeconds: number; explanation: string; }
export interface ProposalPunchInGuidance { trait: "punchInFrequencyPerMinute"; value: number; origin: string; generatorId: string; generatorVersion: string; maximumSuggestions: number; explanation: string; }
export interface EditProposal { id: string; briefId: string; revision: number; status: string; plan: { assetId: string; operations: ProposalOperation[]; warnings: Array<{ severity: string; message: string }>; styleGuidance?: ProposalStyleGuidance | null; punchInGuidance?: ProposalPunchInGuidance | null; noAutomaticChanges: true }; createdAt: string; }
export interface ModelStatus { id: string; version: string; bytes: number; requiredRamBytes: number; ready: boolean; integrity: string; }
export interface ModelsResponse { models: ModelStatus[]; recommended: string; backendAvailable: boolean; detail: string; }
export interface ExportRecord { id: string; timelineId: string; status: string; relativePath: string; format: string; settings: Record<string, unknown>; startedAt: string; completedAt: string | null; checksumSha256: string | null; errorCode: string | null; }

export interface StyleRecipe { id: string; name: string; description: string; version?: string; traits?: ReferenceTraitProfile["traits"]; }
export interface StyleRecipeProposal { recipeId: string; recipeVersion: string; traits: ReferenceTraitProfile["traits"]; requiresReview: true; explanation: string; generatorId: string; generatorVersion: string; }

export interface ModelExecution { provider: string; providerVersion?: string; modelName: string; modelVersion: string; settings: Record<string, number>; executionMs: number; task?: string; }
export interface RuntimeSelection { available: boolean; name?: string; version?: string; settings?: Record<string, number>; detail?: string; code?: string; memoryAdvisory?: string | null; }
export interface RuntimeStatus { available: boolean; provider: string; endpoint?: string; detail?: string; code?: string; models: Array<{ name: string; version: string; sizeBytes: number }>; configuredModels?: Array<{ name: string; task: string; minimumRamBytes: number }>; selections?: Record<string, RuntimeSelection>; }
export interface FootageIndexStatus { available: boolean; stale: boolean; artifactId?: string; schemaVersion?: number; chunkVersion?: number; chunkCount?: number; semantic?: boolean; createdAt?: string; embedding?: { name: string; version: string; provider: string; settings: Record<string, number> } | null; }
export interface FootageFilters { assetId?: string; sourceInTicks?: number; sourceOutTicks?: number; shotId?: string; silence?: boolean; qualityFlags?: Array<"near_black" | "blur">; }
export interface FootageMatch { id: string; assetId: string; transcriptId: string; sourceInTicks: number; sourceOutTicks: number; text: string; score: number; shotId?: string | null; flags: string[]; availableDetectors?: string[]; }

export interface PlanningSelection { candidateId: string; reason: string; confidence: number; confidenceReason: string; requirementIds: string[]; }
export interface PlanningCandidate { id: string; assetId: string; transcriptId: string; sourceInTicks: number; sourceOutTicks: number; excerpt: string; originalText: string; transcriptEdited: boolean; uncertain: boolean; flags: string[]; sourceConfidence: number | null; }
export interface WrittenPlanSection { id: string; title: string; purpose: string; selections: PlanningSelection[]; alternatives: PlanningSelection[]; alternativeNote: string; status: "pending" | "approved" | "rejected"; locked: boolean; confidence: number; requirementsSatisfied: string[]; estimatedDurationTicks: number; }
export interface WrittenPlan { schemaVersion: number; planType: "written-rough-cut"; briefId: string; briefRevision: number; assetId: string; evidenceFingerprint: string; query?: string; searchMode?: "exact" | "semantic"; candidates: PlanningCandidate[]; requirements: { id: string; description: string; kind: string }[]; requiredRanges: { id: string; sourceInTicks: number; sourceOutTicks: number }[]; excludedRanges: { id: string; sourceInTicks: number; sourceOutTicks: number }[]; hook: { candidateId: string | null; reason: string } | null; sections: WrittenPlanSection[]; unmetRequirements: { requirementId: string; reason: string }[]; estimatedDurationTicks: number; targetDurationTicks: number | null; warnings: string[]; provenance: ModelExecution; reviewVersion: number; }
export interface WrittenPlanRecord { id: string; briefId: string; revision: number; status: string; plan: WrittenPlan; stale?: boolean; staleReason?: string | null; }

export type TimelineClip = PersistedProjectState["timelineItems"][number];
export interface TimelineTrack { id: string; kind: "video" | "audio" | "caption"; name: string; }
export interface TimelineMarker { id: string; atTicks: number; label: string; sectionId?: string; planId?: string; }
export interface TimelineDocument { schemaVersion: 2; policies: RevisionPolicies; revision: number; tracks: TimelineTrack[]; clips: TimelineClip[]; captions: NonNullable<PersistedProjectState["captions"]>; markers: TimelineMarker[]; durationTicks: number; canUndo: boolean; canRedo: boolean; }
export interface RevisionPolicies { protectedRanges: {id: string; assetId: string; sourceInTicks: number; sourceOutTicks: number; phrase: string}[]; suppressedCategories: string[]; }
export interface RevisionEvidence { assetId: string; sourceInTicks: number; sourceOutTicks: number; detectorId: string; detectorVersion: string; explanation: string; text?: string; }
export interface RevisionSuggestion { id: string; kind: string; title?: string; operationSummary?: string; evidenceNotes?: string[]; status: string; stale?: boolean; staleReason?: string | null; baseRevision: number; evidenceFingerprint: string; explanation: string; evidence: RevisionEvidence[]; timelineInTicks: number; timelineOutTicks: number; operations: TimelineCommand[]; durationDeltaTicks: number; preview: {before: TimelineClip[]; after: TimelineClip[]}; warnings: string[]; }
export interface RevisionListing { suggestions: RevisionSuggestion[]; revision: number; warnings: string[]; }
export type TimelineCommand =
  | {type: "remove_range"; clipId: string; sourceInTicks: number; sourceOutTicks: number; ripple: boolean}
  | {type: "crop"; clipId: string; cropPreset: "full_frame" | "punch_in_110" | "punch_in_125"}
  | {type: "protect_ranges"; ranges: {assetId: string; sourceInTicks: number; sourceOutTicks: number; phrase: string}[]}
  | {type: "suppress_category"; category: string}
  | { type: "insert"; assetId: string; trackId: string; sourceInTicks: number; sourceOutTicks: number; atTicks?: number; ripple?: boolean }
  | { type: "trim"; clipId: string; sourceInTicks: number; sourceOutTicks: number; ripple?: boolean }
  | { type: "split"; clipId: string; atTicks: number }
  | { type: "delete"; clipId: string; ripple?: boolean }
  | { type: "move"; clipId: string; atTicks: number; trackId?: string }
  | { type: "reorder"; trackId: string; clipIds: string[] }
  | { type: "gap"; trackId: string; atTicks: number; durationTicks: number }
  | { type: "ripple"; trackId: string; atTicks: number; deltaTicks: number }
  | { type: "gain"; clipId: string; gainDb: number }
  | { type: "fades"; clipId: string; fadeInTicks: number; fadeOutTicks: number }
  | { type: "caption"; clipId: string; text: string; sourceInTicks?: number; sourceOutTicks?: number }
  | { type: "edit_caption"; captionId: string; text: string }
  | { type: "marker"; atTicks: number; label: string; markerId?: string }
  | { type: "remove_marker"; markerId: string }
  | { type: "track"; kind: "video" | "audio"; name: string };
