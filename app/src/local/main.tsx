import React from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import { LocalPowerpacksApp } from "./LocalPowerpacksApp";

document.documentElement.classList.add("dark");
document.documentElement.style.colorScheme = "dark";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LocalPowerpacksApp />
  </React.StrictMode>
);
