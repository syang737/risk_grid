import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ModuleRegistry } from "ag-grid-community";
import { AllEnterpriseModule, LicenseManager } from "ag-grid-enterprise";

import App from "./App";
import "./styles.css";

ModuleRegistry.registerModules([AllEnterpriseModule]);

// Without a key the enterprise features still run, with a watermark and a
// console notice. Set VITE_AG_GRID_LICENSE to clear it.
const license = import.meta.env.VITE_AG_GRID_LICENSE;
if (license) LicenseManager.setLicenseKey(license);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
