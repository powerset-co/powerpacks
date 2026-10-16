// ESLint for web/: typescript-eslint strict + stylistic (type-aware), React hooks, a11y.
// Type assertions and non-null assertions are errors: a cast is a documented waiver
// (see LINT-WAIVERS.md), never a habit.
import js from "@eslint/js"
import prettier from "eslint-config-prettier"
import jsxA11y from "eslint-plugin-jsx-a11y"
import reactHooks from "eslint-plugin-react-hooks"
import reactRefresh from "eslint-plugin-react-refresh"
import globals from "globals"
import tseslint from "typescript-eslint"

export default tseslint.config(
  { ignores: ["dist", "node_modules"] },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  ...tseslint.configs.stylisticTypeChecked,
  jsxA11y.flatConfigs.recommended,
  reactHooks.configs.flat.recommended,
  reactRefresh.configs.vite,
  prettier,
  {
    languageOptions: {
      globals: globals.browser,
      parserOptions: {
        projectService: { allowDefaultProject: ["*.js"] },
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      "@typescript-eslint/consistent-type-assertions": ["error", { assertionStyle: "never" }],
      "@typescript-eslint/no-non-null-assertion": "error",
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/consistent-type-definitions": ["error", "interface"],
      "@typescript-eslint/consistent-type-imports": ["error", { fixStyle: "inline-type-imports" }],
      "@typescript-eslint/no-unnecessary-condition": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "@typescript-eslint/switch-exhaustiveness-check": "error",
      "@typescript-eslint/restrict-template-expressions": ["error", { allowNumber: true }],
      // `() => setX(1)` is idiomatic React; the rule's braces add nothing.
      "@typescript-eslint/no-confusing-void-expression": ["error", { ignoreArrowShorthand: true }],
      // React Compiler is not in this build; the rule only warns that a library hook
      // (useVirtualizer) would defeat the compiler's memoization.
      "react-hooks/incompatible-library": "off",
    },
  },
  {
    files: ["**/*.test.ts", "**/*.test.tsx", "src/testing/**"],
    rules: { "@typescript-eslint/no-unsafe-assignment": "off" },
  },
  {
    // Copied as they are from the cloud app (network-search-app) for the Edit Filters modal, so
    // they can be copied again when it changes; its lint is looser than ours (LINT-WAIVERS.md).
    files: [
      "src/pages/agent/EditFiltersModal.tsx",
      "src/pages/agent/AutocompleteInput.tsx",
      "src/components/ui/editable-badge-list.tsx",
      "src/components/ui/checkbox-scroll-list.tsx",
      "src/components/ui/select.tsx",
      "src/components/ui/popover.tsx",
      "src/components/ui/checkbox.tsx",
      "src/components/ui/switch.tsx",
      "src/components/ui/label.tsx",
      "src/components/ui/scroll-area.tsx",
      "src/components/ui/textarea.tsx",
      "src/components/ui/input.tsx",
      "src/lib/agent/companyFilters.ts",
    ],
    rules: {
      "@typescript-eslint/consistent-type-assertions": "off",
      "@typescript-eslint/no-unnecessary-type-assertion": "off",
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/no-unnecessary-condition": "off",
      "@typescript-eslint/no-misused-promises": "off",
      "@typescript-eslint/prefer-nullish-coalescing": "off",
      "@typescript-eslint/prefer-reduce-type-parameter": "off",
      "@typescript-eslint/no-unused-vars": "off",
      "@typescript-eslint/require-await": "off",
      "@typescript-eslint/no-floating-promises": "off",
      "@typescript-eslint/restrict-template-expressions": "off",
      "@typescript-eslint/no-confusing-void-expression": "off",
      "@typescript-eslint/consistent-type-definitions": "off",
      "@typescript-eslint/consistent-indexed-object-style": "off",
      "@typescript-eslint/array-type": "off",
      "@typescript-eslint/prefer-optional-chain": "off",
      "@typescript-eslint/no-unnecessary-type-parameters": "off",
      "jsx-a11y/click-events-have-key-events": "off",
      "jsx-a11y/no-static-element-interactions": "off",
      "jsx-a11y/no-noninteractive-element-interactions": "off",
      "jsx-a11y/label-has-associated-control": "off",
      "react-hooks/exhaustive-deps": "off",
      "react-refresh/only-export-components": "off",
    },
  },
  {
    // Declaration files augment library interfaces; an index signature there cannot be a Record.
    files: ["**/*.d.ts"],
    rules: { "@typescript-eslint/consistent-indexed-object-style": "off" },
  },
  {
    files: ["**/*.js"],
    ...tseslint.configs.disableTypeChecked,
    languageOptions: { globals: globals.node },
  },
  {
    files: ["*.config.ts"],
    languageOptions: { globals: globals.node },
  },
)
