import React from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import { LocalPowerpacksApp } from "./LocalPowerpacksApp";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LocalPowerpacksApp />
  </React.StrictMode>
);
