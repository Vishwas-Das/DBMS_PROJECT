/**
 * Type aliases DERIVED from the generated OpenAPI schema (`schema.d.ts`).
 * Nothing here is a hand-written DTO. Every name points at `components["schemas"][...]`.
 * Regenerate the source with `npm run gen:api`.
 */
import type { components } from "./schema";

type S = components["schemas"];

export type ErrorBody = S["ErrorBody"];
export type ErrorEnvelope = S["ErrorEnvelope"];
export type HealthOut = S["HealthOut"];
export type StatsOut = S["StatsOut"];
export type CustomerOut = S["CustomerOut"];
export type ErasureRequestCreate = S["ErasureRequestCreate"];
export type ErasureRequestOut = S["ErasureRequestOut"];
export type DiscoveryOut = S["DiscoveryOut"];
export type GraphNodeOut = S["GraphNodeOut"];
export type GraphEdgeOut = S["GraphEdgeOut"];
export type EdgeStatusUpdate = S["EdgeStatusUpdate"];
export type SchemaGraphOut = S["SchemaGraphOut"];
export type RecordNodeOut = S["RecordNodeOut"];
export type RecordEdgeOut = S["RecordEdgeOut"];
export type LineageOut = S["LineageOut"];
export type PlanGenerate = S["PlanGenerate"];
export type PlanItemOut = S["PlanItemOut"];
export type PlanConflictOut = S["PlanConflictOut"];
export type PlanOut = S["PlanOut"];
export type PlanApprove = S["PlanApprove"];
export type PlanExecute = S["PlanExecute"];
export type ConflictAcknowledge = S["ConflictAcknowledge"];
export type StepResultOut = S["StepResultOut"];
export type CheckOut = S["CheckOut"];
export type VerificationOut = S["VerificationOut"];
export type ResidualHitOut = S["ResidualHitOut"];
export type ExecutionOut = S["ExecutionOut"];
export type AuditEventOut = S["AuditEventOut"];
export type ChainVerificationOut = S["ChainVerificationOut"];
export type EvidenceOut = S["EvidenceOut"];
export type AuditOut = S["AuditOut"];
export type BenchmarkRunOut = S["BenchmarkRunOut"];

export type ActionCategory = S["ActionCategory"];
export type PlanAction = S["PlanAction"];
export type RecordRole = S["RecordRole"];
export type EdgeOrigin = S["EdgeOrigin"];
export type EdgeStatus = S["EdgeStatus"];
export type Traceability = S["Traceability"];
export type StepStatus = S["StepStatus"];
export type ExecutionMode = S["ExecutionMode"];
export type ExecutionStatus = S["ExecutionStatus"];
export type VerificationOutcome = S["VerificationOutcome"];
export type VerificationStatus = S["VerificationStatus"];
export type RequestStatus = S["RequestStatus"];
export type PlanStatus = S["PlanStatus"];
export type Severity = S["Severity"];
export type CheckType = S["CheckType"];
export type GraphLevel = SchemaGraphOut["level"];
