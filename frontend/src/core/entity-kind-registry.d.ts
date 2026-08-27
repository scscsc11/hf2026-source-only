/**
 * EntityKindRegistry (Spec 020, T6)
 *
 * Central registry of every entity kind the visualizer knows about.
 * Before this registry, adding a new entity kind required touching 9 sites:
 *   - wgs84-projection.ts        : EntityKind data-layer enum
 *   - redis-client.ts            : extractAllEntities() kind recognition
 *   - app.ts                     : updateMultiEntityRenderers() kind dispatch (renderer factory + trail)
 *   - entity-list-panel.ts       : dotClass / labelPrefix ternary chains
 *   - control-panel.ts           : kindToControlType() + ACTIONS_BY_TYPE
 *   - scene.ts                   : SelectableObject.entityType
 *
 * Each kind now lives in ONE descriptor here. The data-layer `EntityKind`
 * enum stays as the single source of truth; this registry attaches the
 * presentation/control metadata to each kind. New entity kinds are added
 * by appending one entry below (plus its renderer factory if it needs a
 * 3D model).
 */
import { EntityKind } from './wgs84-projection';
/** UI-facing entity category used for click routing + 3D pick tagging. */
export type EntityType = 'uav' | 'target' | 'gimbal';
/**
 * Control-panel category. Distinct from EntityType because the control
 * panel distinguishes real targets from decoys (different action sets),
 * whereas the 3D pick path collapses both vehicle kinds onto 'target'.
 */
export type EntityControlType = 'uav' | 'target_vehicle' | 'decoy_vehicle' | 'gimbal';
/** CSS class for the coloured dot in the entity list. */
export type EntityDotClass = 'uav-dot' | 'target-dot' | 'decoy-dot' | 'gimbal-dot';
/** Colour used for this kind's trajectory trail (hex). */
export type TrailKind = 'uav' | 'target' | 'decoy';
/**
 * Static descriptor for one EntityKind. Everything the UI/render layer
 * needs to present + control an entity of this kind, minus the renderer
 * factory (which lives in the App because it owns the WGS84Projection).
 */
export interface EntityDescriptor {
    /** Human-readable short label prefix, e.g. "UAV" / "TARGET". */
    readonly labelPrefix: string;
    /** CSS dot class for list/legend rendering. */
    readonly dotClass: EntityDotClass;
    /** Control-panel category this kind maps onto. */
    readonly controlType: EntityControlType;
    /** Trail category (colour) for this kind's trajectory. */
    readonly trailKind: TrailKind;
    /** UI entity category for 3D pick routing + list click callback. */
    readonly entityType: EntityType;
    /** Short bracket label shown in the control dropdown, e.g. "[UAV]". */
    readonly dropdownLabel: string;
}
/**
 * Lookup the descriptor for a kind. Returns `undefined` for unknown kinds
 * so callers can early-skip (the legacy data path used scattered
 * `kind === 'foo'` ternaries that silently fell through).
 */
export declare function describeEntityKind(kind: EntityKind): EntityDescriptor;
/** True if the registry has a descriptor for this kind. */
export declare function isKnownEntityKind(kind: string): kind is EntityKind;
/**
 * Resolve a raw-frame `type` string (possibly an alias like
 * `fixed_wing_uav`) to the canonical EntityKind, or null if the type is
 * not a recognised entity type. Centralises kind recognition so the
 * frame parser does not carry its own type-string dispatch.
 */
export declare function resolveEntityKind(typeStr: string): EntityKind | null;
/**
 * Map a state EntityKind to the control-panel category. Returns null for
 * unknown kinds (callers should skip them rather than render a control
 * section that has no actions).
 */
export declare function controlTypeForKind(kind: EntityKind): EntityControlType | null;
/**
 * The set of EntityKinds the registry knows about, in declaration order.
 * Useful for tests + for code that needs to iterate all kinds.
 */
export declare const ALL_ENTITY_KINDS: readonly EntityKind[];
//# sourceMappingURL=entity-kind-registry.d.ts.map