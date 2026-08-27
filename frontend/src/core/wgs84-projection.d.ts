import * as THREE from 'three';
export interface WGS84Coord {
    latitude: number;
    longitude: number;
    altitude: number;
}
export interface ENUCoord {
    x: number;
    y: number;
    z: number;
}
export interface UAVState {
    latitude: number;
    longitude: number;
    altitude: number;
    roll: number;
    pitch: number;
    yaw: number;
    velocity: number;
}
export interface GimbalState {
    pan_angle: number;
    tilt_angle: number;
    track_enabled: boolean;
    fov: number;
}
export interface TargetState {
    latitude: number;
    longitude: number;
    altitude: number;
    heading: number;
    speed: number;
}
export interface Waypoint {
    latitude: number;
    longitude: number;
    altitude: number;
}
export interface NavigationPathState {
    waypoints: Waypoint[];
}
export type SimulationStatus = 'running' | 'paused' | 'ended' | 'idle';
export interface SimulationState {
    timestamp: number;
    status: SimulationStatus;
    uav: UAVState;
    gimbal: GimbalState;
    target: TargetState;
    path: NavigationPathState;
    entities?: Record<string, EntityState>;
    zones?: Record<string, unknown>;
}
export interface CommInboxEntry {
    sender: string;
    payload: string;
    recv_time: number;
}
export interface CommState {
    enabled: boolean;
    range_m: number;
    max_bytes: number;
    max_rate_hz: number;
    inbox: CommInboxEntry[];
    stats: {
        sent: number;
        delivered: number;
        received: number;
        rejected_bytes: number;
        rejected_rate: number;
        rejected_range: number;
        rejected_jam: number;
    };
}
export type Team = 'white' | 'red' | 'blue';
export type EventSourceKind = 'kernel' | 'external';
export interface EventSource {
    kind: EventSourceKind;
    producer: string;
    team?: Team | null;
    auth?: unknown;
}
export interface AuthToken {
    token?: string;
    expires_at?: number;
}
export type EventType = 'state.enter_track' | 'state.exit_track' | 'target.discovered' | (string & {});
export interface EventPayloadBase {
    [key: string]: unknown;
}
export interface StateEnterTrackPayload extends EventPayloadBase {
    target_position?: {
        latitude: number;
        longitude: number;
        altitude: number;
    };
    target_type?: string;
    confidence?: number;
}
export interface StateExitTrackPayload extends EventPayloadBase {
    reason?: string;
    last_known_position?: {
        latitude: number;
        longitude: number;
        altitude: number;
    } | null;
}
export interface TargetDiscoveredPayload extends EventPayloadBase {
    target_position?: {
        latitude: number;
        longitude: number;
        altitude: number;
    };
    target_type?: string;
    confidence?: number;
    azimuth_error?: number;
}
export interface SimEvent {
    event_type: EventType;
    source: EventSource;
    entity_uid: string;
    sim_time: number;
    payload: EventPayloadBase;
    event_id?: string;
}
export interface ExtendedDetection {
    detected: boolean;
    confidence: number;
    target_position?: WGS84Coord;
    azimuth_error?: number;
    target_type: string;
    misid_flag: boolean;
    misid_count: number;
    misid_track_duration: number;
}
export type EntityKind = 'uav' | 'ground_vehicle' | 'decoy_vehicle';
export interface EntityState {
    uid: string;
    kind: EntityKind;
    name: string;
    uav?: UAVState;
    gimbal?: GimbalState;
    detection?: ExtendedDetection;
    comm?: CommState;
    vehicle?: TargetState;
}
/**
 * Free-function WGS84 → ENU conversion for code paths that do not own a
 * WGS84Projection instance (e.g. overlay builders and FX that need to
 * project a single (lat, lon) into local frame).  Returns [east, north]
 * in meters using the same WGS84 spherical model as WGS84Projection.
 */
export declare function wgs84ToEnu(latitude: number, longitude: number, referenceLat: number, referenceLon: number): [number, number];
export declare class WGS84Projection {
    private centerLat;
    private centerLon;
    private centerAlt;
    constructor(centerLatitude: number, centerLongitude: number, centerAltitude?: number);
    /**
     * Expose the WGS84 origin (lat, lon) used to define the local ENU frame.
     * Other renderers (e.g. threat-zone overlays) must use the **same**
     * origin or their geometry will be mis-projected by tens to thousands
     * of metres relative to the terrain / entity renderers.
     */
    getCenterLat(): number;
    getCenterLon(): number;
    getCenterAlt(): number;
    /**
     * Convert WGS84 coordinates to ENU (East-North-Up) coordinates
     */
    toENU(latitude: number, longitude: number, altitude: number): ENUCoord;
    /**
     * Convert ENU coordinates to WGS84 coordinates
     */
    toWGS84(x: number, y: number, z: number): WGS84Coord;
    /**
     * Convert WGS84 coordinates to Three.js Vector3
     */
    toVector3(latitude: number, longitude: number, altitude: number): THREE.Vector3;
    /**
     * Convert Three.js Vector3 to WGS84 coordinates
     */
    fromVector3(vector: THREE.Vector3): WGS84Coord;
    private toRad;
    private toDeg;
}
//# sourceMappingURL=wgs84-projection.d.ts.map