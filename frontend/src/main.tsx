import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource-variable/dm-sans";
import "@fontsource-variable/manrope";
import App from "./App";
import "./styles.css";

// StrictMode replays effects in development; hooks must clean up and reject obsolete responses.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
