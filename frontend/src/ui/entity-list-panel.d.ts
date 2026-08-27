import { EntityState } from '../core/wgs84-projection';
import { EntityType } from '../core/entity-kind-registry';
export type { EntityType };
export type EntityClickCallback = (entityType: EntityType, entityId?: string) => void;
export declare class EntityListPanel {
    private container;
    private buttons;
    private onClickCallback;
    private selected;
    private listEl;
    private multiButtons;
    private knownUids;
    private multiSelectedUid;
    constructor(containerId: string);
    private render;
    private select;
    onEntityClick(callback: EntityClickCallback): void;
    highlight(type: EntityType): void;
    /**
     * Spec 017 (FR-017) + Bug 1/2 fixes: rebuild the button list from the
     * multi-entity map. Two critical fixes vs the first implementation:
     *
     *  1. REBUILD ONLY ON CHANGE: we string-compare the sorted uid set and
     *     skip the DOM rebuild when it's identical to last frame. Rebuilding
     *     every frame (60Hz) tore down click handlers before clicks could
     *     fire — that was Bug 1.
     *  2. UID IN LABEL: each button shows the entity's uid alongside its
     *     name so the user can tell "uav_alpha (20001)" from "uav_bravo
     *     (20002)" — that was Bug 2.
     */
    updateFromEntities(entities: Record<string, EntityState>): void;
    private highlightByEntityId;
}
//# sourceMappingURL=entity-list-panel.d.ts.map