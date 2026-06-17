import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";

import { LocalOnboardingPage } from "./LocalOnboardingPage";
import { onboardingHandlers } from "./onboardingStoryMocks";
import {
  HighlightStyles,
  StorybookAppShell,
  linkedinConnectionsCsv,
} from "./storybookPreviewHelpers";

const meta = {
  title: "Local/App Shell",
  parameters: {
    layout: "fullscreen",
    docs: {
      description: {
        component:
          "App shell stories render shared frame components like the sidebar around real app content. Product changes should be made in the imported app components, not in Storybook wrappers.",
      },
    },
  },
} satisfies Meta;

export default meta;

type Story = StoryObj;

export const OnboardingImportWithSidebar: Story = {
  name: "Onboarding import with sidebar",
  render: () => (
    <StorybookAppShell>
      <HighlightStyles />
      <LocalOnboardingPage />
    </StorybookAppShell>
  ),
  parameters: {
    docs: {
      description: {
        story:
          "Uses the real LocalRunSidebar and real LocalOnboardingPage. The story only supplies shell props, mocked API responses, and an in-memory LinkedIn CSV fixture.",
      },
    },
    msw: {
      handlers: onboardingHandlers({
        loggedIn: true,
        envReady: true,
        linkedinStatus: "missing",
        gmail: "connected",
      }),
    },
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(await canvas.findByRole("button", { name: /Import LinkedIn/i }));
    const input = canvasElement.querySelector('input[type="file"]') as HTMLInputElement | null;
    if (!input) throw new Error("LinkedIn CSV file input was not found.");

    const file = new File([linkedinConnectionsCsv(428)], "Connections.csv", { type: "text/csv" });
    await userEvent.upload(input, file);

    const processButton = await canvas.findByRole("button", { name: /^Process$/i });
    processButton.setAttribute("data-story-highlight", "process-button");
    processButton.focus();
  },
};
