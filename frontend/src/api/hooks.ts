/**
 * One TanStack Query hook per API endpoint (module_contracts §10, CR lead-1 §4).
 * Mutations invalidate the query keys whose server state they change. No business logic here:
 * the hooks pass data through unchanged.
 */
import {
  useMutation,
  useMutationState,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { api, ApiError } from "./client";
import type {
  ConflictAcknowledge,
  ErasureRequestCreate,
  ExecutionMode,
  ExecutionOut,
  GraphLevel,
  PlanGenerate,
} from "./types";

export const qk = {
  health: ["health"] as const,
  stats: ["stats"] as const,
  customers: (q: string) => ["customers", q] as const,
  requests: ["erasure-requests"] as const,
  request: (id: string) => ["erasure-requests", id] as const,
  lineage: (requestId: string, level: GraphLevel) => ["lineage", requestId, level] as const,
  lineageAll: (requestId: string) => ["lineage", requestId] as const,
  schemaGraph: (level: GraphLevel) => ["lineage-schema", level] as const,
  plan: (planId: string) => ["erasure-plans", planId] as const,
  verification: (requestId: string) => ["verification", requestId] as const,
  auditEvents: (afterSeq: number, limit: number) => ["audit-events", afterSeq, limit] as const,
  auditEventsAll: ["audit-events"] as const,
  audit: (requestId: string) => ["audit", requestId] as const,
  benchmarks: (scenario: string | null) => ["benchmarks", scenario] as const,
};

export const mk = {
  discovery: (requestId: string) => ["discovery", requestId] as const,
  execute: (planId: string) => ["execute", planId] as const,
  verifyChain: ["verify-chain"] as const,
};

/** Do not retry client-side 4xx errors. One retry for network or 5xx. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < 1;
}

// ------------------------------------------------------------------ queries

export function useHealth() {
  return useQuery({
    queryKey: qk.health,
    queryFn: ({ signal }) => api.get("/api/health", { signal }),
    refetchInterval: 15_000,
  });
}

export function useStats() {
  return useQuery({
    queryKey: qk.stats,
    queryFn: ({ signal }) => api.get("/api/stats", { signal }),
  });
}

export function useCustomers(q: string, enabled = true) {
  return useQuery({
    queryKey: qk.customers(q),
    queryFn: ({ signal }) => api.get("/api/customers", { query: { q, limit: 25 }, signal }),
    enabled,
  });
}

export function useRequests() {
  return useQuery({
    queryKey: qk.requests,
    queryFn: ({ signal }) => api.get("/api/erasure-requests", { query: { limit: 100 }, signal }),
  });
}

export function useRequest(requestId: string | undefined) {
  return useQuery({
    queryKey: qk.request(requestId ?? ""),
    queryFn: ({ signal }) =>
      api.get("/api/erasure-requests/{request_id}", {
        path: { request_id: requestId ?? "" },
        signal,
      }),
    enabled: Boolean(requestId),
  });
}

export function useLineage(requestId: string | undefined, level: GraphLevel) {
  return useQuery({
    queryKey: qk.lineage(requestId ?? "", level),
    queryFn: ({ signal }) =>
      api.get("/api/lineage/{request_id}", {
        path: { request_id: requestId ?? "" },
        query: { level },
        signal,
      }),
    enabled: Boolean(requestId),
  });
}

export function useSchemaGraph(level: GraphLevel, enabled = true) {
  return useQuery({
    queryKey: qk.schemaGraph(level),
    queryFn: ({ signal }) => api.get("/api/lineage/schema", { query: { level }, signal }),
    enabled,
  });
}

export function usePlan(planId: string | null | undefined) {
  return useQuery({
    queryKey: qk.plan(planId ?? ""),
    queryFn: ({ signal }) =>
      api.get("/api/erasure-plans/{plan_id}", { path: { plan_id: planId ?? "" }, signal }),
    enabled: Boolean(planId),
  });
}

export function useVerification(requestId: string | undefined) {
  return useQuery({
    queryKey: qk.verification(requestId ?? ""),
    queryFn: ({ signal }) =>
      api.get("/api/verification/{request_id}", {
        path: { request_id: requestId ?? "" },
        signal,
      }),
    enabled: Boolean(requestId),
  });
}

export function useAuditEvents(afterSeq = 0, limit = 200) {
  return useQuery({
    queryKey: qk.auditEvents(afterSeq, limit),
    queryFn: ({ signal }) =>
      api.get("/api/audit/events", { query: { after_seq: afterSeq, limit }, signal }),
  });
}

export function useAuditForRequest(requestId: string | undefined) {
  return useQuery({
    queryKey: qk.audit(requestId ?? ""),
    queryFn: ({ signal }) =>
      api.get("/api/audit/{request_id}", { path: { request_id: requestId ?? "" }, signal }),
    enabled: Boolean(requestId),
  });
}

export function useBenchmarks(scenario: string | null = null) {
  return useQuery({
    queryKey: qk.benchmarks(scenario),
    queryFn: ({ signal }) =>
      api.get("/api/benchmarks", { query: scenario ? { scenario } : {}, signal }),
  });
}

// ------------------------------------------------------------------ mutations

function invalidateRequest(qc: QueryClient, requestId: string): Promise<unknown> {
  return Promise.all([
    qc.invalidateQueries({ queryKey: qk.request(requestId) }),
    qc.invalidateQueries({ queryKey: qk.requests, exact: true }),
    qc.invalidateQueries({ queryKey: qk.stats }),
  ]);
}

export function useCreateRequest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ErasureRequestCreate) => api.post("/api/erasure-requests", { body }),
    onSuccess: () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: qk.requests }),
        qc.invalidateQueries({ queryKey: qk.stats }),
        qc.invalidateQueries({ queryKey: ["customers"] }),
      ]),
  });
}

export function useRunDiscovery(requestId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: mk.discovery(requestId),
    mutationFn: () =>
      api.post("/api/discovery/{request_id}", { path: { request_id: requestId } }),
    onSuccess: () =>
      Promise.all([
        invalidateRequest(qc, requestId),
        qc.invalidateQueries({ queryKey: qk.lineageAll(requestId) }),
        qc.invalidateQueries({ queryKey: ["lineage-schema"] }),
      ]),
  });
}

export function useGeneratePlan(requestId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body?: PlanGenerate) =>
      api.post("/api/erasure-plans/{request_id}/generate", {
        path: { request_id: requestId },
        body: body ?? null,
      }),
    onSuccess: (plan) => {
      qc.setQueryData(qk.plan(plan.id), plan);
      return Promise.all([
        invalidateRequest(qc, requestId),
        qc.invalidateQueries({ queryKey: qk.lineageAll(requestId) }),
      ]);
    },
  });
}

export function useAcknowledgeConflict(planId: string, requestId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { conflictId: string; body?: ConflictAcknowledge }) =>
      api.post("/api/erasure-plans/{plan_id}/conflicts/{conflict_id}/acknowledge", {
        path: { plan_id: planId, conflict_id: v.conflictId },
        body: v.body ?? null,
      }),
    onSuccess: (plan) => {
      qc.setQueryData(qk.plan(plan.id), plan);
      return Promise.all([
        qc.invalidateQueries({ queryKey: qk.plan(planId) }),
        invalidateRequest(qc, requestId),
      ]);
    },
  });
}

/** Approve sends back the server-provided plan_hash exactly as received. */
export function useApprovePlan(planId: string, requestId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (planHash: string) =>
      api.post("/api/erasure-plans/{plan_id}/approve", {
        path: { plan_id: planId },
        body: { plan_hash: planHash },
      }),
    onSuccess: (plan) => {
      qc.setQueryData(qk.plan(plan.id), plan);
      return Promise.all([
        qc.invalidateQueries({ queryKey: qk.plan(planId) }),
        invalidateRequest(qc, requestId),
        qc.invalidateQueries({ queryKey: qk.auditEventsAll }),
      ]);
    },
  });
}

export function useExecutePlan(planId: string, requestId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: mk.execute(planId),
    mutationFn: (mode: ExecutionMode) =>
      api.post("/api/erasure-plans/{plan_id}/execute", {
        path: { plan_id: planId },
        body: { mode },
      }),
    onSettled: () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: qk.plan(planId) }),
        invalidateRequest(qc, requestId),
        qc.invalidateQueries({ queryKey: qk.verification(requestId) }),
        qc.invalidateQueries({ queryKey: qk.audit(requestId) }),
        qc.invalidateQueries({ queryKey: qk.auditEventsAll }),
        qc.invalidateQueries({ queryKey: qk.health }),
      ]),
  });
}

/**
 * The contract has no GET for past executions, so the monitor shows the results of execute
 * calls made in this browser session (TanStack mutation cache). See CR frontend-engineer-1.
 */
export function useExecutionResults(planId: string | null | undefined): ExecutionOut[] {
  return useMutationState({
    filters: { mutationKey: mk.execute(planId ?? ""), status: "success" },
    select: (m) => m.state.data as ExecutionOut,
  });
}

export function useRunVerification(requestId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post("/api/verification/{request_id}/run", { path: { request_id: requestId } }),
    onSuccess: (v) => {
      qc.setQueryData(qk.verification(requestId), v);
      return Promise.all([
        invalidateRequest(qc, requestId),
        qc.invalidateQueries({ queryKey: qk.audit(requestId) }),
        qc.invalidateQueries({ queryKey: qk.auditEventsAll }),
      ]);
    },
  });
}

export function useVerifyChain() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: mk.verifyChain,
    mutationFn: () => api.post("/api/audit/verify-chain"),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.health }),
  });
}

export function useDownloadEvidence() {
  return useMutation({
    mutationFn: (requestId: string) =>
      api.getBlob("/api/evidence/{request_id}/download", { path: { request_id: requestId } }),
  });
}

export function useDownloadEvidenceLeaves() {
  return useMutation({
    mutationFn: (requestId: string) =>
      api.getBlob("/api/evidence/{request_id}/leaves", { path: { request_id: requestId } }),
  });
}
