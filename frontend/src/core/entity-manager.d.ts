import * as THREE from 'three';
import { SceneManager } from '../rendering/scene';
import { WGS84Projection, EntityState } from '../core/wgs84-projection';
import { SimulationState } from '../core/state-manager';
import { TerrainRenderer } from '../rendering/terrain';
/** Toggle readers the manager inherits from the UI at spawn time. */
export interface EntityManagerToggles {
    /** Current trail-visibility checkbox state. */
    trailVisible: () => boolean;
    /** Current gimbal-FOV checkbox state. */
    gimbalFovVisible: () => boolean;
}
export declare class EntityManager {
    private sceneManager;
    private projection;
    private terrain;
    private toggles;
    private renderers;
    private trails;
    private uavIndicators;
    constructor(sceneManager: SceneManager, projection: WGS84Projection, terrain: TerrainRenderer, toggles: EntityManagerToggles);
    /** Reconcile the per-entity maps with the current frame's entities. */
    update(entities: Record<string, EntityState>): void;
    /** Propagate the trail-visibility toggle to all trails. */
    setTrailsVisible(visible: boolean): void;
    /** Propagate the FOV toggle to all UAV gimbal indicators. */
    setGimbalFovVisible(visible: boolean): void;
    /**
     * Resolve a selected entity's 3D position by unique_id (multi-entity
     * path). Returns the origin if the id is not in the map.
     */
    positionForEntity(entityId: string | null | undefined, state: SimulationState): THREE.Vector3;
    private spawnRenderer;
}
//# sourceMappingURL=entity-manager.d.ts.map