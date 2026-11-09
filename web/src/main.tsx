import React from "react";
import { createRoot } from "react-dom/client";
import { AccountLogin } from "./AccountAuth";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {location.pathname === "/connect" ? <AccountLogin native /> :
      location.pathname === "/invite" ? <AccountLogin invite /> : <App />}
  </React.StrictMode>,
);
