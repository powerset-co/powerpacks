import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";

import { LocalOnboardingPage } from "./LocalOnboardingPage";
import { LocalOnboardingV2Page } from "./LocalOnboardingV2Page";
import { onboardingHandlers } from "./onboardingStoryMocks";
import { HighlightStyles, StorybookPreview, linkedinConnectionsCsv } from "./storybookPreviewHelpers";

// These stories are preview harnesses only. Do not fork onboarding/sidebar copy
// or layout here; edit the app components imported below so the production UI
// and Storybook stay identical.
const meta = {
  title: "Local/Onboarding",
  parameters: {
    layout: "fullscreen",
    docs: {
      description: {
        component:
          "These stories render the real app components with mocked local API calls. If copy, layout, or behavior needs to change, edit the underlying app components in app/src/local, not the Storybook wrapper or mocks.",
      },
    },
  },
} satisfies Meta;

export default meta;

type Story = StoryObj;

export const FreshInstall: Story = {
  name: "Fresh install",
  render: () => (
    <StorybookPreview>
      <LocalOnboardingPage />
    </StorybookPreview>
  ),
  parameters: {
    msw: {
      handlers: onboardingHandlers({
        loggedIn: false,
        envReady: false,
        linkedinStatus: "missing",
        gmail: "empty",
      }),
    },
  },
};

export const LoggedInReadyToImport: Story = {
  name: "Logged in, ready to import",
  render: () => (
    <StorybookPreview>
      <LocalOnboardingPage />
    </StorybookPreview>
  ),
  parameters: {
    msw: {
      handlers: onboardingHandlers({
        loggedIn: true,
        envReady: true,
        linkedinStatus: "missing",
        gmail: "connected",
      }),
    },
  },
};

export const LinkedInCsvSelected: Story = {
  name: "LinkedIn CSV selected",
  render: () => (
    <StorybookPreview>
      <HighlightStyles />
      <LocalOnboardingPage />
    </StorybookPreview>
  ),
  parameters: {
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

export const ImportCompleted: Story = {
  name: "Import completed",
  render: () => (
    <StorybookPreview>
      <LocalOnboardingPage />
    </StorybookPreview>
  ),
  parameters: {
    msw: {
      handlers: onboardingHandlers({
        loggedIn: true,
        envReady: true,
        linkedinStatus: "completed",
        gmail: "connected",
      }),
    },
  },
};

export const FullFlowWithGmailStep: Story = {
  name: "V2 full flow with Gmail",
  render: () => (
    <StorybookPreview>
      <LocalOnboardingV2Page />
    </StorybookPreview>
  ),
  parameters: {
    msw: {
      handlers: onboardingHandlers({
        loggedIn: true,
        envReady: true,
        linkedinStatus: "completed",
        gmail: "pending",
      }),
    },
  },
};
