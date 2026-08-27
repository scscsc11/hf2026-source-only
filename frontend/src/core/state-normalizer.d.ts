import { SimulationState } from './state-manager';
import { EntityState } from './wgs84-projection';
export declare function normalizeSimState(message: unknown, entityIds?: Record<string, string>): SimulationState | null;
export declare function extractAllEntities(raw: Record<string, unknown>): Record<string, EntityState>;
//# sourceMappingURL=state-normalizer.d.ts.map