import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "src/api/schema.gen.ts"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { ecmaVersion: 2022, globals: globals.browser },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Demo data is dev-only: only the API layer may import it (vite.config.ts also fails a build that bundles it).
      "no-restricted-imports": ["error", { patterns: [{ group: ["**/api/demo", "**/api/demo/*", "@/api/demo*"], message: "Demo data is dev-only; go through `api` (src/api/index.ts)." }] }],
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  { files: ["src/api/index.ts", "src/api/demo/**"], rules: { "no-restricted-imports": "off" } },
  { files: ["vite.config.ts", "scripts/**"], languageOptions: { globals: globals.node } },
);
