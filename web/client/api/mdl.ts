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

export type MdlKnowledgeKind = 'qa_sql' | 'business_rule' | 'glossary' | 'analysis_rule';

export type MdlKnowledgeStatus = 'draft' | 'approved';

export type MdlKnowledgeValidationMode = 'wren_plan' | 'wren_plan_explain' | 'result_checked' | string;

export type MdlKnowledgeValidation = {
  mode: MdlKnowledgeValidationMode;
  data_source?: string;
  validated_at?: string;
  reviewed_by?: string;
};

export type MdlKnowledgeTerm = {
  term: string;
  meaning: string;
  aliases: string[];
};

export type MdlKnowledgeRule = {
  metric: string;
  conditions: string[];
  guidance: string;
  checks?: MdlKnowledgeRuleCheck[];
  report_requirements?: string[];
};

export type MdlKnowledgeRuleCheck = {
  column: string;
  operator: string;
  value?: string | number | boolean | null | Array<string | number | boolean | null>;
  severity: 'error' | 'warning';
  message: string;
};

export type MdlKnowledgeExpectedResult = {
  columns: string[];
  rows: Array<Array<string | number | boolean | null>>;
};

export type MdlKnowledgeEntry = {
  id: string;
  kind: MdlKnowledgeKind;
  title: string;
  question?: string;
  content: string;
  sql?: string;
  aliases: string[];
  models: Record<string, string[]>;
  terms?: MdlKnowledgeTerm[];
  rule?: MdlKnowledgeRule;
  expected_result?: MdlKnowledgeExpectedResult;
  reuse_on_exact_match?: boolean;
  status: MdlKnowledgeStatus;
  mdl_fingerprint?: string;
  stale?: boolean;
  validation?: MdlKnowledgeValidation;
  exact_match?: boolean;
  conflict?: boolean;
  score?: number;
};

export type MdlKnowledgeCorpus = {
  database?: string;
  revision?: number;
  entries: MdlKnowledgeEntry[];
};

export type MdlKnowledgeSavePayload = {
  database?: string;
  revision?: number;
  entries: MdlKnowledgeEntry[];
};

export type MdlKnowledgeApprovePayload = {
  database?: string;
  id: string;
  revision?: number;
};

export type MdlKnowledgeSearchPayload = {
  database?: string;
  question: string;
  top_k?: number;
};

export type MdlKnowledgeSearchMatch = MdlKnowledgeEntry & {
  score?: number;
  exact_match?: boolean;
  conflict?: boolean;
};

export type MdlKnowledgeSearchResponse = {
  database?: string;
  matches: MdlKnowledgeSearchMatch[];
};

export type MdlKnowledgeSpaceBinding = {
  id?: string;
  title: string;
  space_name: string;
  description: string;
  models: Record<string, string[]>;
  users: string[];
  roles: string[];
  status: MdlKnowledgeStatus;
  mdl_fingerprint?: string;
  space_fingerprint?: string;
  stale?: boolean;
  validation?: MdlKnowledgeValidation;
};

export type MdlKnowledgeSpaceCorpus = {
  database: string;
  revision: number;
  spaces: MdlKnowledgeSpaceBinding[];
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

export const getMdlKnowledge = (database?: string) =>
  GET<{ database?: string } | undefined, MdlKnowledgeCorpus>(
    '/api/v1/mdl/knowledge',
    database ? { database } : undefined,
  );

export const saveMdlKnowledge = (data: MdlKnowledgeSavePayload) =>
  PUT<MdlKnowledgeSavePayload, MdlKnowledgeCorpus>('/api/v1/mdl/knowledge', data);

export const approveMdlKnowledge = (data: MdlKnowledgeApprovePayload) =>
  POST<MdlKnowledgeApprovePayload, MdlKnowledgeCorpus>('/api/v1/mdl/knowledge/approve', data);

export const searchMdlKnowledge = (data: MdlKnowledgeSearchPayload) =>
  POST<MdlKnowledgeSearchPayload, MdlKnowledgeSearchResponse>('/api/v1/mdl/knowledge/search', data);

export const getMdlKnowledgeSpaces = (database: string) =>
  GET<{ database: string }, MdlKnowledgeSpaceCorpus>('/api/v1/mdl/knowledge-spaces', { database });

export const saveMdlKnowledgeSpaces = (data: MdlKnowledgeSpaceCorpus) =>
  PUT<MdlKnowledgeSpaceCorpus, MdlKnowledgeSpaceCorpus>('/api/v1/mdl/knowledge-spaces', data);

export const approveMdlKnowledgeSpace = (data: { database: string; revision: number; id: string }) =>
  POST<typeof data, MdlKnowledgeSpaceCorpus>('/api/v1/mdl/knowledge-spaces/approve', data);

export const searchMdlKnowledgeSpaces = (data: { database: string }) =>
  POST<typeof data, { database: string; spaces: string[] }>('/api/v1/mdl/knowledge-spaces/search', data);
