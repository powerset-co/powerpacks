import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "@/styles/index.css";
import { PeoplePage } from "./PeoplePage";

const root = document.getElementById("root");
if (!root) throw new Error("People page needs a #root element");

const queryClient = new QueryClient();

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <PeoplePage />
    </QueryClientProvider>
  </StrictMode>,
);
