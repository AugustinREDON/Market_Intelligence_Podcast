import type { ComponentType } from 'react';
import type { Manifest } from '../content/types';

/**
 * Extension point for future dashboard widgets shown beneath the podcast player
 * (market indices, bond yields, FX & commodities, top movers, earnings calendar,
 * breaking news). Each module receives the manifest and should render a <Panel>.
 *
 * Intentionally empty in v1: no live prices or news are shown, and no empty
 * "coming soon" panels are rendered. Example (see README → Extending):
 *
 *   import { MarketIndicesPanel } from '../modules/MarketIndicesPanel';
 *   export const sidebarModules = [MarketIndicesPanel];
 */
export type SidebarModule = ComponentType<{ manifest: Manifest }>;

export const sidebarModules: SidebarModule[] = [];
