import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { ContentProvider } from './state/content';
import { PlayerProvider } from './state/player';
import { ThemeProvider } from './state/theme';
import './styles/tokens.css';
import './styles/base.css';
import './styles/components.css';
import './styles/forest-home.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <ContentProvider>
        <PlayerProvider>
          <App />
        </PlayerProvider>
      </ContentProvider>
    </ThemeProvider>
  </StrictMode>,
);
