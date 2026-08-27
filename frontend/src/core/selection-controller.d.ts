import { SceneManager } from '../rendering/scene';
import { StateManager } from './state-manager';
import { EntityManager } from './entity-manager';
import { EntityInfoPanel, SelectedEntity } from '../ui/entity-info-panel';
import { EntityListPanel } from '../ui/entity-list-panel';
import { EntityType } from './entity-kind-registry';
export declare class SelectionController {
    private sceneManager;
    private stateManager;
    private entityManager;
    private entityInfoPanel;
    private entityListPanel;
    selectedEntity: SelectedEntity;
    selectedUid: string | null;
    constructor(sceneManager: SceneManager, stateManager: StateManager, entityManager: EntityManager, entityInfoPanel: EntityInfoPanel, entityListPanel: EntityListPanel);
    /** Handle an entity pick (from list, 3D click, or control panel). */
    select(entityType: EntityType, entityId?: string): void;
    /** Follow-target refresh on every state tick (call from App). */
    refreshFollow(): void;
    /** Drop the selection if the selected uid is no longer present. */
    reconcileWithState(): void;
}
//# sourceMappingURL=selection-controller.d.ts.map