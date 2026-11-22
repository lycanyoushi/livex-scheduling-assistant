import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

const config = [
  { ignores: [".next/**", "next-env.d.ts", ".venv/**"] },
  ...nextCoreWebVitals,
  ...nextTypescript,
];

export default config;
