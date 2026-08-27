import * as THREE from 'three';
import { WGS84Projection } from '../core/wgs84-projection';
export interface TerrainConfig {
    visible: boolean;
}
export interface HeightmapData {
    width: number;
    height: number;
    elevations: number[];
    vertexColors: number[];
    contours: number[][][];
    bounds: {
        minLatitude: number;
        maxLatitude: number;
        minLongitude: number;
        maxLongitude: number;
    };
}
export declare class TerrainRenderer {
    private mesh;
    private wireframe;
    private contourGroup;
    private projection;
    private config;
    private loaded;
    private heightmapData;
    constructor(projection: WGS84Projection, config?: Partial<TerrainConfig>);
    /**
     * Load heightmap data from a JSON URL and create the terrain mesh.
     */
    load(url: string): Promise<void>;
    /**
     * Create the Three.js terrain mesh from heightmap data.
     *
     * Each vertex is positioned by converting its (lat, lon, elevation) to
     * the ENU Three.js coordinate system via WGS84Projection, ensuring the
     * terrain aligns perfectly with other scene entities.
     */
    private createTerrain;
    /**
     * Create contour line geometry from precomputed contour polylines.
     */
    private createContours;
    /**
     * Map elevation to a sci-fi / tactical color ramp: ascending from dark blue
     * to bright cyan as elevation rises (120–280 m, based on HeightSample.csv).
     */
    private elevationToColor;
    /**
     * Get the terrain mesh, or null if not loaded yet.
     */
    getMesh(): THREE.Mesh | null;
    /**
     * Get the contour line group, or null if not loaded yet.
     */
    getContourGroup(): THREE.Group | null;
    /**
     * Returns true if terrain data has been loaded and mesh created.
     */
    isLoaded(): boolean;
    /**
     * Toggle terrain visibility.
     */
    setVisible(visible: boolean): void;
    /**
     * Get current visibility state.
     */
    isVisible(): boolean;
    /**
     * Query terrain elevation at a given (latitude, longitude) using bilinear interpolation.
     * Returns elevation in meters, or 0 if terrain is not loaded or point is out of bounds.
     */
    getElevation(latitude: number, longitude: number): number;
}
//# sourceMappingURL=terrain.d.ts.map