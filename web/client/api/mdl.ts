import { GET, POST, PUT } from '.';

export type MdlColumn = {
  name: string;
  type?: string;
  display_name?: string;
  description?: string;
  expression?: string;
  properties?: Record<string, any>;
  isCalculated?: boolean;
  semantic_type?: 'dimension' | 'measure' | 'attribute' | string;
  visible?: boolean;
  enabled?: boolean;
};

export type MdlModel = {
  name: string;
  display_name?: string;
  description?: string;
  table?: string;
  schema?: string;
  tableReference?: { schema?: string; table: string };
  ref_sql?: string;
  columns?: MdlColumn[];
  primary_key?: string;
  enabled?: boolean;
};

export type MdlRelation = {
  name: string;
  models?: string[];
  type?: string;
  join_type?: string;
  condition?: string;
  enabled?: boolean;
};

export type MdlDocument = {
  revision?: number;
  status?: string;
  source?: string;
  manifest?: MdlManifest;
  policies?: MdlPolicy[];
  database?: string;
  enabled?: boolean;
  models?: MdlModel[];
  relations?: MdlRelation[];
  relationships?: MdlRelation[];
  [key: string]: any;
};

export type MdlManifest = {
  models?: MdlModel[];
  relations?: MdlRelation[];
  relationships?: MdlRelation[];
  [key: string]: any;
};

export type MdlSchemaTable = {
  name: string;
  schema?: string;
  type?: string;
  columns?: MdlColumn[];
};

export type MdlSchemaResponse = {
  tables?: MdlSchemaTable[];
  models?: MdlModel[];
  relationships?: MdlRelation[];
};

export type MdlPolicy = {
  id?: string;
  username?: string;
  user?: string;
  users?: string[];
  models?: string[] | Record<string, { columns?: string[]; filters?: MdlPolicyFilter[] }>;
  tables?: string[];
  columns?: Record<string, string[]>;
  row_filter?: string;
  region_filter?: string;
  filters?: MdlPolicyFilter[];
  enabled?: boolean;
};

export type MdlPolicyFilter = {
  column: string;
  op: string;
  value: string;
};

export type MdlPoliciesResponse = {
  policies?: MdlPolicy[];
  revision?: number;
  database?: string;
};

export type MdlValidationIssue = {
  level?: 'error' | 'warning' | string;
  message: string;
  path?: string;
};

export type MdlValidationResponse = {
  valid: boolean;
  revision?: number;
  issues?: MdlValidationIssue[];
};

export type MdlSavePayload = {
  revision?: number;
  database?: string;
  manifest: MdlManifest;
};

export type MdlQueryPayload = {
  sql: string;
  limit?: number;
  database?: string;
};

export type MdlQueryResponse = {
  columns?: string[];
  rows?: Record<string, any>[] | any[][];
  count?: number;
  truncated?: boolean;
  elapsed_ms?: number;
  logical_sql?: string;
};

export type MdlSessionUser = {
  username: string;
  role: string;
};

export type MdlSessionResponse = {
  user: MdlSessionUser | null;
  enabled?: boolean;
  detail?: string;
};

export const loginMdl = (data: { token: string }) =>
  POST<{ token: string }, MdlSessionResponse>('/api/v1/mdl/login', data);

export const getMdlSession = () => GET<null, MdlSessionResponse>('/api/v1/mdl/session');

export const logoutMdl = () => POST<null, MdlSessionResponse>('/api/v1/mdl/logout');

export const getMdl = (database?: string) => GET<{ database?: string }, MdlDocument>('/api/v1/mdl', { database });

export const saveMdl = (data: MdlSavePayload) => PUT<MdlSavePayload, MdlDocument>('/api/v1/mdl', data);

export const validateMdl = (data: MdlSavePayload) =>
  POST<MdlSavePayload, MdlValidationResponse>('/api/v1/mdl/validate', data);

export const getMdlSchema = (database?: string) =>
  GET<{ database?: string } | undefined, MdlSchemaResponse>('/api/v1/mdl/schema', database ? { database } : undefined);

export const getMdlPolicies = (database?: string) =>
  GET<{ database?: string } | undefined, MdlPoliciesResponse>(
    '/api/v1/mdl/policies',
    database ? { database } : undefined,
  );

export const saveMdlPolicies = (data: MdlPoliciesResponse) =>
  PUT<MdlPoliciesResponse, MdlPoliciesResponse>('/api/v1/mdl/policies', data);

export const queryMdl = (data: MdlQueryPayload) => POST<MdlQueryPayload, MdlQueryResponse>('/api/v1/mdl/query', data);
