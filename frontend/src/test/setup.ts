import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import { clearCredentials } from "../api/credentials";

afterEach(() => {
  cleanup();
  clearCredentials();
  window.sessionStorage.clear();
});
