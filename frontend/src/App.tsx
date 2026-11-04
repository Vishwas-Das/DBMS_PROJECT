import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, useState } from "react";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import { shouldRetry } from "./api/hooks";
import { Layout } from "./components/Layout";
import { EmptyState } from "./components/States";

// Route-level code splitting: React Flow + dagre load only with the pages that use them.
const DashboardPage = lazy(() =>
  import("./pages/DashboardPage").then((m) => ({ default: m.DashboardPage })),
);
const RequestsPage = lazy(() =>
  import("./pages/RequestsPage").then((m) => ({ default: m.RequestsPage })),
);
const LineagePage = lazy(() =>
  import("./pages/LineagePage").then((m) => ({ default: m.LineagePage })),
);
const PlanPage = lazy(() =>
  import("./pages/PlanPage").then((m) => ({ default: m.PlanPage })),
);
const ExecutionPage = lazy(() =>
  import("./pages/ExecutionPage").then((m) => ({ default: m.ExecutionPage })),
);
const VerificationPage = lazy(() =>
  import("./pages/VerificationPage").then((m) => ({ default: m.VerificationPage })),
);
const BenchmarksPage = lazy(() =>
  import("./pages/BenchmarksPage").then((m) => ({ default: m.BenchmarksPage })),
);

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: shouldRetry, staleTime: 5_000, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="requests" element={<RequestsPage />} />
        <Route path="lineage" element={<LineagePage />} />
        <Route path="lineage/:requestId" element={<LineagePage />} />
        <Route path="plan" element={<PlanPage />} />
        <Route path="plan/:requestId" element={<PlanPage />} />
        <Route path="execution" element={<ExecutionPage />} />
        <Route path="execution/:requestId" element={<ExecutionPage />} />
        <Route path="verification" element={<VerificationPage />} />
        <Route path="verification/:requestId" element={<VerificationPage />} />
        <Route path="benchmarks" element={<BenchmarksPage />} />
        <Route
          path="*"
          element={
            <EmptyState title="Page not found">
              <Link to="/" className="link">
                Back to the dashboard
              </Link>
            </EmptyState>
          }
        />
      </Route>
    </Routes>
  );
}

export function App() {
  const [client] = useState(makeQueryClient);
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
