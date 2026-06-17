import type { Preview } from "@storybook/react-vite";
import { initialize, mswLoader } from "msw-storybook-addon";

import "../src/index.css";

initialize({ onUnhandledRequest: "bypass" });

const preview: Preview = {
  loaders: [mswLoader],
  parameters: {
    layout: "fullscreen",
  },
};

export default preview;
