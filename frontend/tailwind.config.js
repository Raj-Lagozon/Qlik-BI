/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#eef4ff",
          100: "#dbe6fe",
          200: "#bccffd",
          300: "#8eaefa",
          400: "#5a84f5",
          500: "#3660ee",
          600: "#2444e2",
          700: "#1d34c8",
          800: "#1e2ea1",
          900: "#1e2c7f",
        },
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};
