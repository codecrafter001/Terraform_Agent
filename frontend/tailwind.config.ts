import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        // Light theme: white/near-white surfaces, brand blue accents.
        background: "#ffffff",
        surface: "#f7f8fb",
        "surface-light": "#eef1f7",
        border: "#e2e5ee",
        // Sampled directly from the AIKart logo (frontend/public/aikart-logo.jpeg):
        // swoosh = rgb(0,55,255), wordmark = rgb(0,0,4). brand-600 is the exact
        // logo blue; the rest of the scale is generated around it.
        brand: {
          50: "#eef2ff",
          100: "#e0e7ff",
          200: "#c7d2fe",
          300: "#a3b3fd",
          400: "#6b83fa",
          500: "#335cff",
          600: "#0038ff",
          700: "#002ecc",
          800: "#002299",
          900: "#001766",
          950: "#000c33",
        },
        // Near-black navy from the logo's "kart" wordmark - use for headings/ink
        // text instead of pure black, matching the logo's exact tone.
        ink: "#04070f",
        aws: {
          ec2: "#0038ff",
          vpc: "#8b5cf6",
          s3: "#10b981",
          rds: "#f59e0b",
          sg: "#ef4444",
          iam: "#ec4899",
        }
      },
    },
  },
  plugins: [],
};
export default config;
