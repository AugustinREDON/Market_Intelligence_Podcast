import {
  createContext,
  useContext,
  useLayoutEffect,
  type ReactNode,
} from 'react';

export type ThemeId = 'mip';

export const THEMES = [
  {
    id: 'mip' as const,
    label: 'MIP',
    description: 'Market Intelligence Podcast',
    swatch: '#ffd600',
  },
];

const theme: ThemeId = 'mip';

const Ctx = createContext<{
  theme: ThemeId;
  setTheme: (theme: ThemeId) => void;
} | null>(null);

// A single theme is active. Retain this interface for a future
// light/dark selector; switching behaviour will be implemented then.
function setTheme(_theme: ThemeId) {}

export function ThemeProvider({ children }: { children: ReactNode }) {
  useLayoutEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute('content', '#0d2b59');
  }, []);

  return (
    <Ctx.Provider value={{ theme, setTheme }}>
      {children}
    </Ctx.Provider>
  );
}

export function useTheme() {
  const context = useContext(Ctx);
  if (!context) throw new Error('useTheme outside ThemeProvider');
  return context;
}