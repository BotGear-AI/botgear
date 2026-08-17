// managed
// Api Client interface for use across the project
export interface ApiClient {
  id?: string;
  name?: string;
  secret?: string;
  roles?: string[];
  created?: number;
}

export function createEmptyApiClient(): ApiClient {
  return {
    id: undefined,
    name: undefined,
    secret: undefined,
    roles: [],
    created: undefined,
  };
}

// Helper function to validate ApiClient object
export function isApiClient(obj: any): obj is ApiClient {
  return obj && typeof obj === 'object';
}

// Helper function to clone ApiClient object
export function cloneApiClient(item: ApiClient): ApiClient {
  return {
    ...item,
  };
}

// JSON Schema for Fastify route validation and OpenAPI documentation
export const ApiClientSchema = {
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "example": "client-123e4567-e89b-12d3-a456-426614174000"
    },
    "name": {
      "type": "string",
      "description": "Human-readable label, e.g. the integration or owner that uses this client",
      "example": "Reporting Pipeline"
    },
    "secret": {
      "type": "string",
      "description": "Shared secret presented in the X-API-Key header; the row authenticates when it matches. Minted server-side on create when omitted",
      "example": "supersecret"
    },
    "roles": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "description": "Roles granted to requests authenticated by this client; an Admin-roled key is a full admin credential",
      "example": [
        "Admin"
      ]
    },
    "created": {
      "type": "integer",
      "description": "Creation time, epoch milliseconds",
      "example": 1683123456789
    }
  }
}
