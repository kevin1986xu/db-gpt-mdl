import {
  MdlColumn,
  MdlDocument,
  MdlModel,
  MdlPoliciesResponse,
  MdlPolicy,
  MdlRelation,
  MdlSchemaTable,
  getMdl,
  getMdlSchema,
  getMdlSession,
  loginMdl,
  logoutMdl,
  queryMdl,
  saveMdl,
  saveMdlPolicies,
  validateMdl,
} from '@/client/api/mdl';
import { getDbList } from '@/client/api/request';
import { apiInterceptors } from '@/client/api/tools/interceptors';
import type { IChatDbSchema } from '@/types/db';
import { PRODUCT_NAME } from '@/utils/brand';
import {
  CheckCircleOutlined,
  DeleteOutlined,
  DownloadOutlined,
  ExclamationCircleOutlined,
  FileTextOutlined,
  ImportOutlined,
  LinkOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import type { UploadProps } from 'antd';
import {
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Upload,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import MdlKnowledge from './MdlKnowledge';

type SelectedModelState = Record<string, { enabled: boolean; columns: Record<string, boolean> }>;

type RelationForm = {
  name: string;
  models: string[];
  type?: string;
  condition?: string;
  enabled?: boolean;
};

type PolicyForm = {
  user: string;
  models: string[];
  columnsByModel?: Record<string, string[]>;
  region_filter?: string;
  enabled?: boolean;
};

const emptyMdl: MdlDocument = {
  revision: undefined,
  status: 'draft',
  models: [],
  relations: [],
};

type Session = {
  username: string;
  role: string;
};

type SessionState = {
  user: Session | null;
  enabled: boolean;
  detail?: string;
};

type NormalizedMdlColumn = MdlColumn & {
  display_name: string;
  description: string;
  visible: boolean;
  enabled: boolean;
};

const relationTypes = ['many_to_one', 'one_to_many', 'one_to_one', 'many_to_many'];
const reactAgentMdlDemoUrl = '/?chat_mode=chat_react_agent&database_name=mdl_demo';

function normalizeColumns(columns?: MdlColumn[]): NormalizedMdlColumn[] {
  return (columns ?? []).map(column => ({
    ...column,
    display_name: column.display_name ?? column.properties?.displayName ?? '',
    description: column.description ?? column.properties?.description ?? '',
    visible: column.visible ?? column.enabled ?? true,
    enabled: column.enabled ?? column.visible ?? true,
  }));
}

function normalizeModel(model: MdlModel): MdlModel {
  const tableReference = model.tableReference;
  return {
    ...model,
    table: model.table ?? tableReference?.table,
    schema: model.schema ?? tableReference?.schema,
    enabled: model.enabled ?? true,
    columns: normalizeColumns(model.columns),
  };
}

function normalizePolicy(policy: MdlPolicy): MdlPolicy {
  const models = policy.models ?? policy.tables ?? [];
  return {
    ...policy,
    enabled: policy.enabled ?? true,
    user: policy.user ?? policy.username ?? policy.users?.[0] ?? '',
    username: policy.username ?? policy.user ?? policy.users?.[0] ?? '',
    models,
    row_filter: policy.row_filter ?? policy.region_filter ?? '',
  };
}

function modelLabel(model: MdlModel) {
  return model.display_name || model.name;
}

function policyUsername(policy: MdlPolicy) {
  return policy.user || policy.username || policy.users?.[0] || '';
}

function policyModelNames(policy: MdlPolicy) {
  if (Array.isArray(policy.models)) return policy.models;
  if (policy.models) return Object.keys(policy.models);
  return policy.tables ?? [];
}

function policyModelConfig(policy: MdlPolicy, modelName: string) {
  if (!policy.models || Array.isArray(policy.models)) return undefined;
  return policy.models[modelName];
}

function policyRegionValue(policy: MdlPolicy) {
  const firstModelName = policyModelNames(policy)[0];
  const firstFilter = firstModelName ? policyModelConfig(policy, firstModelName)?.filters?.[0] : undefined;
  return policy.row_filter ?? policy.region_filter ?? firstFilter?.value ?? '';
}

function buildSelection(models: MdlModel[]): SelectedModelState {
  return models.reduce<SelectedModelState>((acc, model) => {
    acc[model.name] = {
      enabled: model.enabled ?? true,
      columns: normalizeColumns(model.columns).reduce<Record<string, boolean>>((columnAcc, column) => {
        columnAcc[column.name] = column.visible ?? column.enabled ?? true;
        return columnAcc;
      }, {}),
    };
    return acc;
  }, {});
}

function applySelection(models: MdlModel[], selection: SelectedModelState): MdlModel[] {
  return models.map(model => {
    const modelSelection = selection[model.name];
    if (!modelSelection) return normalizeModel(model);
    return {
      ...normalizeModel(model),
      enabled: modelSelection.enabled,
      columns: normalizeColumns(model.columns).map(column => ({
        ...column,
        visible: modelSelection.columns[column.name] ?? true,
        enabled: modelSelection.columns[column.name] ?? true,
      })),
    };
  });
}

function schemaToModels(schemaTables: MdlSchemaTable[]): MdlModel[] {
  return schemaTables.map(table => ({
    name: table.name,
    table: table.name,
    schema: table.schema,
    tableReference: { schema: table.schema ?? 'main', table: table.name },
    display_name: table.name,
    enabled: true,
    columns: normalizeColumns(table.columns),
  }));
}

function downloadJson(filename: string, value: unknown) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function MdlWorkbench() {
  const [mdl, setMdl] = useState<MdlDocument>(emptyMdl);
  const [schemaTables, setSchemaTables] = useState<MdlSchemaTable[]>([]);
  const [dataSources, setDataSources] = useState<IChatDbSchema[]>([]);
  const [selectedDatabase, setSelectedDatabase] = useState<string>();
  const [schemaLoading, setSchemaLoading] = useState(false);
  const adminLoadId = useRef(0);
  const [addModelOpen, setAddModelOpen] = useState(false);
  const [addModelTable, setAddModelTable] = useState<string>();
  const [policies, setPolicies] = useState<MdlPolicy[]>([]);
  const [selection, setSelection] = useState<SelectedModelState>({});
  const [selectedModelName, setSelectedModelName] = useState<string>();
  const [relationForm] = Form.useForm<RelationForm>();
  const [policyForm] = Form.useForm<PolicyForm>();
  const selectedPolicyModels = ((Form.useWatch('models', policyForm) ?? []) as string[]).filter(Boolean);
  const [editingPolicyUsername, setEditingPolicyUsername] = useState<string>();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validationIssues, setValidationIssues] = useState<string[]>([]);
  const [jsonOpen, setJsonOpen] = useState(false);
  const [jsonText, setJsonText] = useState('');
  const [sessionState, setSessionState] = useState<SessionState>({ user: null, enabled: true });
  const [sessionLoading, setSessionLoading] = useState(true);
  const [token, setToken] = useState('');
  const [loginLoading, setLoginLoading] = useState(false);
  const [sql, setSql] = useState('select * from orders limit 20');
  const [sqlLimit, setSqlLimit] = useState(20);
  const [querying, setQuerying] = useState(false);
  const [queryResult, setQueryResult] = useState<{
    columns: string[];
    rows: any[][];
    count?: number;
    truncated?: boolean;
    elapsed_ms?: number;
    logical_sql?: string;
  }>({ columns: [], rows: [] });

  const models = useMemo(() => (mdl.models ?? []).map(normalizeModel), [mdl.models]);
  const relations = useMemo(() => mdl.relations ?? [], [mdl.relations]);
  const selectedModel = useMemo(
    () => models.find(model => model.name === selectedModelName) ?? models[0],
    [models, selectedModelName],
  );

  const modelOptions = useMemo(() => models.map(model => ({ label: modelLabel(model), value: model.name })), [models]);

  const selectedModelColumns = useMemo(() => normalizeColumns(selectedModel?.columns), [selectedModel]);
  const session = sessionState.user;
  const isAdmin = session?.role === 'admin';
  const canUseMdl = sessionState.enabled !== false;

  const currentManifest = useCallback(
    () => ({
      ...(mdl.manifest ?? {}),
      models: applySelection(models, selection),
      relationships: relations,
    }),
    [mdl.manifest, models, relations, selection],
  );

  const loadAdminData = useCallback(async (database?: string) => {
    const loadId = ++adminLoadId.current;
    setLoading(true);
    setSchemaLoading(true);
    try {
      const [[mdlError, remoteMdl, mdlResponse], [, dbList]] = await Promise.all([
        apiInterceptors(getMdl(database)),
        apiInterceptors(getDbList()),
      ]);
      if (mdlError || !mdlResponse?.success || !remoteMdl) return;
      const nextDataSources = dbList ?? [];
      const defaultDatabase = database || remoteMdl.database || nextDataSources[0]?.db_name || nextDataSources[0]?.name;
      const [, schema] = await apiInterceptors(getMdlSchema(defaultDatabase));
      if (loadId !== adminLoadId.current) return;

      const nextSchemaTables = schema?.tables ?? [];
      const remoteManifest = remoteMdl?.manifest ?? {};
      const manifestModels = remoteManifest.models ?? remoteMdl?.models ?? [];
      const nextModels = manifestModels.map(normalizeModel);
      const nextRelationships =
        remoteManifest.relationships ?? remoteManifest.relations ?? remoteMdl?.relationships ?? [];
      const nextMdl = {
        ...emptyMdl,
        ...remoteMdl,
        manifest: remoteManifest,
        models: nextModels,
        relations: nextRelationships,
      };

      setMdl(nextMdl);
      setDataSources(nextDataSources);
      setSelectedDatabase(defaultDatabase);
      setSchemaTables(nextSchemaTables);
      setPolicies((remoteMdl.policies ?? []).map(normalizePolicy));
      setSelection(buildSelection(nextModels));
      setSelectedModelName(current => nextModels.find(model => model.name === current)?.name ?? nextModels[0]?.name);
      setAddModelTable(undefined);
      setValidationIssues([]);
    } finally {
      if (loadId === adminLoadId.current) {
        setLoading(false);
        setSchemaLoading(false);
      }
    }
  }, []);

  const loadAnalystData = useCallback(async () => {
    setLoading(true);
    try {
      const [, schema] = await apiInterceptors(getMdlSchema());
      const nextSchemaTables = schema?.tables ?? [];
      const nextModels = (schema?.models?.length ? schema.models : schemaToModels(nextSchemaTables)).map(
        normalizeModel,
      );
      const nextRelationships = schema?.relationships ?? [];

      setMdl({
        ...emptyMdl,
        status: 'visible',
        models: nextModels,
        relations: nextRelationships,
      });
      setSelectedDatabase(undefined);
      setDataSources([]);
      setSchemaTables(nextSchemaTables);
      setPolicies([]);
      setSelection(buildSelection(nextModels));
      setSelectedModelName(nextModels[0]?.name);
      setValidationIssues([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadByRole = useCallback(
    async (nextSession: Session, database?: string) => {
      if (nextSession.role === 'admin') {
        await loadAdminData(database);
        return;
      }
      await loadAnalystData();
    },
    [loadAdminData, loadAnalystData],
  );

  useEffect(() => {
    let mounted = true;
    const loadSession = async () => {
      setSessionLoading(true);
      const [error, result] = await apiInterceptors(getMdlSession(), '*');
      if (mounted && !error) {
        setSessionState({ user: result?.user ?? null, enabled: result?.enabled ?? true, detail: result?.detail });
        if (result?.user && result?.enabled !== false) await loadByRole(result.user);
      }
      if (mounted) setSessionLoading(false);
    };
    loadSession();
    return () => {
      mounted = false;
    };
  }, [loadByRole]);

  const handleLogin = async () => {
    if (!token.trim()) {
      message.warning('请输入 token');
      return;
    }
    setLoginLoading(true);
    try {
      const [error, result] = await apiInterceptors(loginMdl({ token: token.trim() }));
      if (!error && result?.user) {
        setSessionState({ user: result.user, enabled: result.enabled ?? true, detail: result.detail });
        setToken('');
        if (result.enabled === false) {
          message.warning(result.detail || 'MDL 当前未启用');
        } else {
          await loadByRole(result.user);
          message.success(`已登录 ${result.user.username}`);
        }
      }
    } finally {
      setLoginLoading(false);
    }
  };

  const handleLogout = async () => {
    await apiInterceptors(logoutMdl());
    setSessionState({ user: null, enabled: true });
    setMdl(emptyMdl);
    setPolicies([]);
    setSelection({});
  };

  const updateModelEnabled = (modelName: string, enabled: boolean) => {
    setSelection(prev => ({
      ...prev,
      [modelName]: {
        enabled,
        columns: prev[modelName]?.columns ?? {},
      },
    }));
  };

  const updateColumnEnabled = (modelName: string, columnName: string, enabled: boolean) => {
    setSelection(prev => ({
      ...prev,
      [modelName]: {
        enabled: prev[modelName]?.enabled ?? true,
        columns: {
          ...(prev[modelName]?.columns ?? {}),
          [columnName]: enabled,
        },
      },
    }));
  };

  const updateColumnAnnotation = (modelName: string, columnName: string, patch: Partial<MdlColumn>) => {
    setMdl(prev => ({
      ...prev,
      models: (prev.models ?? []).map(model => {
        if (model.name !== modelName) return model;
        return {
          ...model,
          columns: normalizeColumns(model.columns).map(column => {
            if (column.name !== columnName) return column;
            const next = { ...column, ...patch };
            if (patch.description !== undefined || patch.display_name !== undefined) {
              next.properties = {
                ...(column.properties ?? {}),
                ...(patch.description !== undefined ? { description: patch.description } : {}),
                ...(patch.display_name !== undefined ? { displayName: patch.display_name } : {}),
              };
            }
            return next;
          }),
        };
      }),
    }));
  };

  const removeColumnFromModel = (modelName: string, columnName: string) => {
    Modal.confirm({
      title: '从问数模型移除字段',
      content: `字段「${columnName}」只会从 MDL 语义模型中移除，不会删除数据库字段。继续吗？`,
      okText: '移除字段',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => {
        setMdl(prev => ({
          ...prev,
          models: (prev.models ?? []).map(model =>
            model.name === modelName
              ? { ...model, columns: normalizeColumns(model.columns).filter(column => column.name !== columnName) }
              : model,
          ),
        }));
        setPolicies(prev =>
          prev.map(policy => {
            if (!policy.models || Array.isArray(policy.models)) return policy;
            const modelPolicy = policy.models[modelName];
            if (!modelPolicy) return policy;
            const columns = modelPolicy.columns?.filter(column => column !== columnName) ?? [];
            const filters = modelPolicy.filters?.filter(filter => filter.column !== columnName) ?? [];
            const nextModels = { ...policy.models };
            if (columns.length === 0) {
              delete nextModels[modelName];
            } else {
              nextModels[modelName] = { ...modelPolicy, columns, filters };
            }
            return {
              ...policy,
              models: nextModels,
            };
          }),
        );
        setSelection(prev => {
          const next = { ...prev };
          if (next[modelName]) {
            const columns = { ...next[modelName].columns };
            delete columns[columnName];
            next[modelName] = { ...next[modelName], columns };
          }
          return next;
        });
        message.success(`字段 ${columnName} 已从 MDL 模型移除`);
      },
    });
  };

  const removeModel = (modelName: string) => {
    setMdl(prev => ({
      ...prev,
      models: (prev.models ?? []).filter(model => model.name !== modelName),
      relations: (prev.relations ?? []).filter(relation => !(relation.models ?? []).includes(modelName)),
    }));
    setSelection(prev => {
      const next = { ...prev };
      delete next[modelName];
      return next;
    });
    setSelectedModelName(current => (current === modelName ? undefined : current));
  };

  const switchDatabase = async (database?: string) => {
    if (!database || database === selectedDatabase) return;
    const resetAndLoad = async () => {
      await loadAdminData(database);
    };
    if (models.length > 0) {
      Modal.confirm({
        title: '切换数据源',
        content: '切换数据源会清空当前未保存的模型、关联关系和策略，是否继续？',
        okText: '继续切换',
        cancelText: '取消',
        onOk: resetAndLoad,
      });
      return;
    }
    await resetAndLoad();
  };

  const handleAddModel = () => {
    const table = schemaTables.find(item => item.name === addModelTable);
    if (!table) {
      message.warning('请先选择数据表');
      return;
    }
    if (models.some(model => model.name === table.name)) {
      message.warning('该表已经添加为模型');
      return;
    }
    const nextModel: MdlModel = {
      name: table.name,
      display_name: table.name,
      table: table.name,
      schema: table.schema,
      tableReference: { schema: table.schema ?? 'main', table: table.name },
      enabled: true,
      columns: normalizeColumns(table.columns),
    };
    setMdl(prev => ({ ...prev, database: selectedDatabase, models: [...(prev.models ?? []), nextModel] }));
    setSelection(prev => ({
      ...prev,
      [nextModel.name]: {
        enabled: true,
        columns: normalizeColumns(nextModel.columns).reduce<Record<string, boolean>>((acc, column) => {
          acc[column.name] = true;
          return acc;
        }, {}),
      },
    }));
    setSelectedModelName(nextModel.name);
    setAddModelOpen(false);
    setAddModelTable(undefined);
    message.success(`已添加模型 ${table.name}`);
  };

  const handleAddRelation = async () => {
    const values = await relationForm.validateFields();
    setMdl(prev => ({
      ...prev,
      relations: [
        ...(prev.relations ?? []),
        {
          name: values.name,
          models: values.models,
          type: values.type,
          condition: values.condition,
          enabled: values.enabled ?? true,
        },
      ],
    }));
    relationForm.resetFields();
  };

  const handleRemoveRelation = (name: string) => {
    setMdl(prev => ({ ...prev, relations: (prev.relations ?? []).filter(relation => relation.name !== name) }));
  };

  const handleAddPolicy = async () => {
    const values = await policyForm.validateFields();
    const username = values.user.trim();
    const modelPolicy = values.models.reduce<
      Record<string, { columns: string[]; filters: { column: string; op: string; value: string }[] }>
    >((acc, modelName) => {
      const model = models.find(item => item.name === modelName);
      const columns = values.columnsByModel?.[modelName]?.length
        ? values.columnsByModel[modelName]
        : normalizeColumns(model?.columns).map(column => column.name);
      acc[modelName] = {
        columns,
        filters: values.region_filter
          ? [
              {
                column: 'region',
                op: 'eq',
                value: values.region_filter,
              },
            ]
          : [],
      };
      return acc;
    }, {});
    setPolicies(prev => [
      ...prev.filter(policy => policyUsername(policy) !== username && policyUsername(policy) !== editingPolicyUsername),
      {
        id: username,
        username,
        user: username,
        models: modelPolicy,
        row_filter: values.region_filter,
        region_filter: values.region_filter,
        enabled: values.enabled ?? true,
      },
    ]);
    policyForm.resetFields();
    setEditingPolicyUsername(undefined);
  };

  const handleRemovePolicy = (id?: string) => {
    setPolicies(prev => prev.filter(policy => (policy.id ?? policyUsername(policy)) !== id));
  };

  const handleEditPolicy = (policy: MdlPolicy) => {
    const modelNames = policyModelNames(policy);
    const columnsByModel = modelNames.reduce<Record<string, string[]>>((acc, modelName) => {
      acc[modelName] = policyModelConfig(policy, modelName)?.columns ?? [];
      return acc;
    }, {});
    policyForm.setFieldsValue({
      user: policyUsername(policy),
      models: modelNames,
      columnsByModel,
      region_filter: policyRegionValue(policy),
      enabled: policy.enabled ?? true,
    });
    setEditingPolicyUsername(policyUsername(policy));
  };

  const handleValidate = async () => {
    setValidating(true);
    try {
      const [, result] = await apiInterceptors(
        validateMdl({ revision: mdl.revision, database: selectedDatabase, manifest: currentManifest() }),
      );
      const issues =
        result?.issues?.map(
          issue => `${issue.level ?? 'info'}: ${issue.path ? `${issue.path} ` : ''}${issue.message}`,
        ) ?? [];
      setValidationIssues(issues);
      if (result?.valid) message.success('MDL 校验通过');
      if (result && !result.valid) message.warning('MDL 校验存在问题');
    } finally {
      setValidating(false);
    }
  };

  const handleSave = async () => {
    if (!isAdmin) {
      message.warning('只有管理员可以保存 MDL 配置');
      return;
    }
    setSaving(true);
    try {
      const nextManifest = currentManifest();
      const [saveError, savedMdl, saveResponse] = await apiInterceptors(
        saveMdl({ revision: mdl.revision, database: selectedDatabase, manifest: nextManifest }),
      );
      if (saveError || !saveResponse?.success || !savedMdl) {
        message.error('MDL 保存失败，字段标注尚未保存。当前编辑已保留，请处理错误后重试。');
        return;
      }
      const savedManifest = savedMdl.manifest ?? nextManifest;
      const savedModels = (savedManifest.models ?? savedMdl.models ?? nextManifest.models ?? []).map(normalizeModel);
      const savedRelationships = savedManifest.relationships ?? savedManifest.relations ?? savedMdl.relationships ?? [];
      setMdl({
        ...savedMdl,
        database: savedMdl.database ?? selectedDatabase,
        manifest: savedManifest,
        models: savedModels,
        relations: savedRelationships,
        revision: savedMdl.revision,
      });
      const persistedPolicies = savedMdl.policies ?? policies;
      const enabledPolicies = persistedPolicies.filter(policy => policy.enabled !== false);
      const [policyError, savedPolicies, policyResponse] = await apiInterceptors(
        saveMdlPolicies({
          policies: enabledPolicies,
          revision: savedMdl.revision,
          database: savedMdl.database ?? selectedDatabase,
        } as MdlPoliciesResponse),
      );
      if (policyError || !policyResponse?.success || !savedPolicies) {
        message.warning('模型和字段标注已保存，访问策略保存失败，请处理错误后重试。');
        return;
      }
      if (savedPolicies?.policies) setPolicies(savedPolicies.policies.map(normalizePolicy));
      if (typeof savedPolicies?.revision === 'number') {
        setMdl(prev => ({ ...prev, revision: savedPolicies.revision }));
      }
      message.success('MDL 配置已保存');
    } finally {
      setSaving(false);
    }
  };

  const handleImportJson: UploadProps['beforeUpload'] = file => {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const nextMdl = JSON.parse(String(reader.result)) as MdlDocument;
        const nextManifest = nextMdl.manifest ?? nextMdl;
        const nextModels = (nextManifest.models ?? nextMdl.models ?? []).map(normalizeModel);
        setMdl({
          ...emptyMdl,
          ...nextMdl,
          manifest: nextManifest,
          models: nextModels,
          relations: nextManifest.relationships ?? nextManifest.relations ?? nextMdl.relations ?? [],
        });
        setSelectedDatabase(nextMdl.database ?? selectedDatabase);
        setSelection(buildSelection(nextModels));
        setSelectedModelName(nextModels[0]?.name);
        setValidationIssues([]);
        message.success('MDL JSON 已导入');
      } catch {
        message.error('JSON 格式无效');
      }
    };
    reader.readAsText(file);
    return false;
  };

  const handleQuery = async () => {
    if (!sql.trim()) {
      message.warning('请输入 SQL');
      return;
    }
    setQuerying(true);
    try {
      const [error, result] = await apiInterceptors(
        queryMdl({ sql: sql.trim(), limit: sqlLimit, database: selectedDatabase }),
      );
      if (error || !result) return;
      const columns =
        result.columns ??
        (Array.isArray(result.rows) && result.rows.length > 0 && !Array.isArray(result.rows[0])
          ? Object.keys(result.rows[0] as Record<string, any>)
          : []);
      const rows = (result.rows ?? []).map(row =>
        Array.isArray(row) ? row : columns.map(column => (row as Record<string, any>)[column]),
      );
      setQueryResult({
        columns,
        rows,
        count: result.count,
        truncated: result.truncated,
        elapsed_ms: result.elapsed_ms,
        logical_sql: result.logical_sql,
      });
    } finally {
      setQuerying(false);
    }
  };

  const modelColumns: ColumnsType<MdlModel> = [
    {
      title: '模型',
      dataIndex: 'name',
      render: (_, model) => (
        <Button type='link' onClick={() => setSelectedModelName(model.name)} className='px-0'>
          {modelLabel(model)}
        </Button>
      ),
    },
    { title: '物理表', dataIndex: 'table', render: value => value || '-' },
    {
      title: '字段',
      render: (_, model) => normalizeColumns(model.columns).length,
      width: 90,
    },
    {
      title: '可见',
      width: 90,
      render: (_, model) => (
        <Switch
          size='small'
          checked={selection[model.name]?.enabled ?? true}
          disabled={!isAdmin}
          onChange={checked => updateModelEnabled(model.name, checked)}
        />
      ),
    },
    {
      title: '操作',
      width: 82,
      render: (_, model) => (
        <Button
          danger
          type='link'
          icon={<DeleteOutlined />}
          disabled={!isAdmin}
          onClick={() => removeModel(model.name)}
        >
          移除
        </Button>
      ),
    },
  ];

  const relationColumns: ColumnsType<MdlRelation> = [
    { title: '关系名', dataIndex: 'name' },
    { title: '模型', dataIndex: 'models', render: value => (value ?? []).join(' -> ') },
    { title: '类型', dataIndex: 'type', render: value => value || '-' },
    { title: '条件', dataIndex: 'condition', ellipsis: true },
    {
      title: '操作',
      width: 90,
      render: (_, relation) => (
        <Button danger type='link' disabled={!isAdmin} onClick={() => handleRemoveRelation(relation.name)}>
          移除
        </Button>
      ),
    },
  ];

  const policyColumns: ColumnsType<MdlPolicy> = [
    {
      title: '用户',
      dataIndex: 'user',
      render: (_, policy) => policyUsername(policy) || policy.users?.join(', ') || '-',
    },
    {
      title: '模型',
      dataIndex: 'models',
      render: (_, policy) => policyModelNames(policy).join(', '),
    },
    {
      title: '字段',
      render: (_, policy) =>
        policyModelNames(policy)
          .map(modelName => {
            const count = policyModelConfig(policy, modelName)?.columns?.length;
            return `${modelName}:${count ?? 'all'}`;
          })
          .join(' / '),
    },
    {
      title: '区域值',
      dataIndex: 'row_filter',
      render: (_, policy) => policyRegionValue(policy) || '-',
    },
    {
      title: '启用',
      width: 80,
      render: (_, policy, index) => (
        <Switch
          size='small'
          checked={policy.enabled ?? true}
          disabled={!isAdmin}
          onChange={checked =>
            setPolicies(prev =>
              prev.map((item, itemIndex) => (itemIndex === index ? { ...item, enabled: checked } : item)),
            )
          }
        />
      ),
    },
    {
      title: '操作',
      width: 130,
      render: (_, policy) => (
        <Space>
          <Button type='link' disabled={!isAdmin} onClick={() => handleEditPolicy(policy)}>
            编辑
          </Button>
          <Button
            danger
            type='link'
            disabled={!isAdmin}
            onClick={() => handleRemovePolicy(policy.id ?? policyUsername(policy))}
          >
            移除
          </Button>
        </Space>
      ),
    },
  ];

  const annotationColumns: ColumnsType<NormalizedMdlColumn> = [
    {
      title: '字段',
      dataIndex: 'name',
      fixed: 'left',
      width: 150,
      render: (value, column) => (
        <div>
          <div className='font-medium'>{value}</div>
          {column.type && <div className='text-xs text-gray-400'>{column.type}</div>}
        </div>
      ),
    },
    {
      title: '权限',
      width: 75,
      render: (_, column) => (
        <Switch
          size='small'
          checked={selection[selectedModel?.name ?? '']?.columns?.[column.name] ?? true}
          disabled={!isAdmin}
          onChange={checked => selectedModel && updateColumnEnabled(selectedModel.name, column.name, checked)}
        />
      ),
    },
    {
      title: '显示名称',
      width: 170,
      render: (_, column) => (
        <Input
          size='small'
          value={column.display_name ?? ''}
          disabled={!isAdmin}
          placeholder='例如：订单金额'
          onChange={event =>
            selectedModel &&
            updateColumnAnnotation(selectedModel.name, column.name, { display_name: event.target.value })
          }
        />
      ),
    },
    {
      title: '字段标注 / 描述',
      width: 260,
      render: (_, column) => (
        <Input.TextArea
          autoSize={{ minRows: 1, maxRows: 3 }}
          value={column.description ?? ''}
          disabled={!isAdmin}
          placeholder='说明业务含义、口径或敏感级别'
          onChange={event =>
            selectedModel &&
            updateColumnAnnotation(selectedModel.name, column.name, { description: event.target.value })
          }
        />
      ),
    },
    {
      title: '语义类型',
      width: 130,
      render: (_, column) => (
        <Select
          size='small'
          className='w-full'
          allowClear
          value={column.semantic_type}
          disabled={!isAdmin}
          placeholder='未标注'
          options={[
            { label: '维度', value: 'dimension' },
            { label: '指标', value: 'measure' },
            { label: '属性', value: 'attribute' },
          ]}
          onChange={value =>
            selectedModel && updateColumnAnnotation(selectedModel.name, column.name, { semantic_type: value })
          }
        />
      ),
    },
    {
      title: '表达式',
      width: 230,
      render: (_, column) => (
        <Input
          size='small'
          value={column.expression ?? ''}
          disabled={!isAdmin}
          placeholder='可选，如 customer.name'
          onChange={event =>
            selectedModel &&
            updateColumnAnnotation(selectedModel.name, column.name, {
              expression: event.target.value,
              isCalculated: Boolean(event.target.value),
            })
          }
        />
      ),
    },
    {
      title: '操作',
      fixed: 'right',
      width: 72,
      render: (_, column) => (
        <Tooltip title='从问数模型移除，不影响数据库'>
          <Button
            danger
            type='text'
            size='small'
            icon={<DeleteOutlined />}
            disabled={!isAdmin}
            aria-label={`从问数模型移除 ${column.name}`}
            onClick={() => selectedModel && removeColumnFromModel(selectedModel.name, column.name)}
          />
        </Tooltip>
      ),
    },
  ];

  return (
    <div className='h-[calc(100vh-64px)] overflow-y-auto px-6 pb-8'>
      {!sessionLoading && !session ? (
        <Card className='mx-auto mt-12 max-w-lg' title='MDL 管理登录'>
          <Alert
            className='mb-4'
            type='info'
            showIcon
            message='请输入本地 MDL 管理 token'
            description='登录后，cookie 会用于问数查询和配置权限校验。'
          />
          <Space.Compact block>
            <Input.Password
              value={token}
              onChange={event => setToken(event.target.value)}
              onPressEnter={handleLogin}
              placeholder='MDL token'
            />
            <Button type='primary' loading={loginLoading} onClick={handleLogin}>
              登录
            </Button>
          </Space.Compact>
        </Card>
      ) : (
        <>
          <div className='flex flex-wrap items-center justify-between gap-3 mb-4'>
            <div>
              <div className='flex items-center gap-2 text-xl font-semibold text-gray-900 dark:text-gray-100'>
                <FileTextOutlined className='text-blue-600' />
                问数配置
              </div>
              <div className='text-sm text-gray-500 mt-1'>管理{PRODUCT_NAME}可查询的数据、字段、关联关系和访问权限</div>
            </div>
            <Space wrap>
              {isAdmin && dataSources.length > 0 && (
                <Select
                  className='min-w-[220px]'
                  value={selectedDatabase}
                  loading={schemaLoading}
                  disabled={loading || saving}
                  placeholder='选择数据源'
                  options={dataSources.map(source => ({
                    label: source.name || source.db_name,
                    value: source.db_name || source.name,
                  }))}
                  onChange={switchDatabase}
                />
              )}
              {isAdmin && (
                <>
                  <Upload accept='application/json,.json' showUploadList={false} beforeUpload={handleImportJson}>
                    <Button icon={<ImportOutlined />}>导入 JSON</Button>
                  </Upload>
                  <Button icon={<DownloadOutlined />} onClick={() => downloadJson('mdl.json', currentManifest())}>
                    导出 JSON
                  </Button>
                </>
              )}
              <Button
                icon={<ReloadOutlined />}
                disabled={!canUseMdl || loading || saving}
                onClick={() => session && loadByRole(session, selectedDatabase)}
              >
                刷新
              </Button>
              {isAdmin && (
                <>
                  <Button icon={<CheckCircleOutlined />} loading={validating} onClick={handleValidate}>
                    校验
                  </Button>
                  <Button
                    type='primary'
                    icon={<SaveOutlined />}
                    loading={saving}
                    disabled={loading}
                    onClick={handleSave}
                  >
                    保存
                  </Button>
                </>
              )}
            </Space>
          </div>

          <Spin spinning={loading || sessionLoading}>
            <div className='mb-3 flex items-center justify-between text-sm text-gray-500'>
              <span>
                当前用户：<Tag color={isAdmin ? 'gold' : 'blue'}>{session?.username}</Tag>
                <Tag>{session?.role}</Tag>
              </span>
              <Button type='link' onClick={handleLogout}>
                退出登录
              </Button>
            </div>
            {!isAdmin && (
              <Alert
                className='mb-4'
                type={canUseMdl ? 'info' : 'error'}
                showIcon
                message={canUseMdl ? '当前为只读模式，只有管理员可以保存 MDL 和访问策略。' : 'MDL 当前未启用'}
                description={
                  canUseMdl ? (
                    <Link href={reactAgentMdlDemoUrl}>返回 React-agent 问数入口</Link>
                  ) : (
                    sessionState.detail || '请联系管理员启用本地 MDL 服务。'
                  )
                }
              />
            )}
            <Row gutter={[16, 16]}>
              <Col xs={24} md={8} lg={6}>
                <Card size='small'>
                  <Statistic title='Revision' value={mdl.revision || 'draft'} />
                </Card>
              </Col>
              <Col xs={24} md={8} lg={6}>
                <Card size='small'>
                  <Statistic title='Status' value={mdl.status || 'draft'} />
                </Card>
              </Col>
              <Col xs={24} md={8} lg={6}>
                <Card size='small'>
                  <Statistic title='Models' value={models.length} />
                </Card>
              </Col>
              <Col xs={24} md={8} lg={6}>
                <Card size='small'>
                  <Statistic title='Policies' value={policies.length} />
                </Card>
              </Col>
            </Row>

            {validationIssues.length > 0 && (
              <Alert
                className='mt-4'
                type='warning'
                showIcon
                icon={<ExclamationCircleOutlined />}
                message='校验结果'
                description={validationIssues.map(issue => (
                  <div key={issue}>{issue}</div>
                ))}
              />
            )}

            <Tabs
              className='mt-4'
              items={[
                {
                  key: 'models',
                  label: '模型与字段',
                  children: (
                    <Row gutter={[16, 16]}>
                      <Col xs={24} xl={12}>
                        <Card
                          title='模型清单'
                          size='small'
                          extra={
                            <Button
                              type='primary'
                              ghost
                              icon={<ImportOutlined />}
                              disabled={!selectedDatabase || schemaLoading}
                              onClick={() => setAddModelOpen(true)}
                            >
                              手动添加模型
                            </Button>
                          }
                        >
                          <Table
                            rowKey='name'
                            size='small'
                            pagination={false}
                            dataSource={models}
                            columns={modelColumns}
                            locale={{
                              emptyText: <Empty description='此数据源暂无保存的模型，请导入 JSON 或手动添加模型' />,
                            }}
                          />
                        </Card>
                      </Col>
                      <Col xs={24} xl={12}>
                        <Card title={selectedModel ? `${modelLabel(selectedModel)} 字段权限` : '字段权限'} size='small'>
                          {selectedModel ? (
                            <div className='space-y-3'>
                              <div className='flex flex-wrap gap-2'>
                                <Tag color='blue'>{selectedModel.table || selectedModel.name}</Tag>
                                <Tag>{selectedModelColumns.length} columns</Tag>
                              </div>
                              <Row gutter={[12, 12]}>
                                <Col xs={24} md={10}>
                                  <div className='mb-1 text-xs text-gray-500'>模型显示名称</div>
                                  <Input
                                    disabled={!isAdmin}
                                    value={selectedModel.display_name ?? ''}
                                    placeholder='例如：订单明细'
                                    onChange={event =>
                                      setMdl(prev => ({
                                        ...prev,
                                        models: (prev.models ?? []).map(model =>
                                          model.name === selectedModel.name
                                            ? { ...model, display_name: event.target.value }
                                            : model,
                                        ),
                                      }))
                                    }
                                  />
                                </Col>
                                <Col xs={24} md={14}>
                                  <div className='mb-1 text-xs text-gray-500'>模型说明</div>
                                  <Input
                                    disabled={!isAdmin}
                                    value={selectedModel.description ?? ''}
                                    placeholder='描述模型的业务口径和使用范围'
                                    onChange={event =>
                                      setMdl(prev => ({
                                        ...prev,
                                        models: (prev.models ?? []).map(model =>
                                          model.name === selectedModel.name
                                            ? { ...model, description: event.target.value }
                                            : model,
                                        ),
                                      }))
                                    }
                                  />
                                </Col>
                              </Row>
                              <Divider className='my-3' />
                              <Table
                                rowKey='name'
                                size='small'
                                pagination={false}
                                scroll={{ x: 1000 }}
                                dataSource={selectedModelColumns}
                                columns={annotationColumns}
                                locale={{ emptyText: <Empty description='暂无字段' /> }}
                              />
                            </div>
                          ) : (
                            <Empty description='暂无模型' />
                          )}
                        </Card>
                      </Col>
                    </Row>
                  ),
                },
                ...(isAdmin
                  ? [
                      {
                        key: 'relations',
                        label: (
                          <span>
                            <LinkOutlined /> 关联关系
                          </span>
                        ),
                        children: (
                          <Row gutter={[16, 16]}>
                            <Col xs={24} xl={16}>
                              <Card title='已配置关系' size='small'>
                                <Table
                                  rowKey='name'
                                  size='small'
                                  pagination={false}
                                  dataSource={relations}
                                  columns={relationColumns}
                                />
                              </Card>
                            </Col>
                            <Col xs={24} xl={8}>
                              <Card title='新增关系' size='small'>
                                <Form
                                  form={relationForm}
                                  disabled={!isAdmin}
                                  layout='vertical'
                                  initialValues={{ enabled: true, type: relationTypes[0] }}
                                >
                                  <Form.Item
                                    name='name'
                                    label='关系名'
                                    rules={[{ required: true, message: '请输入关系名' }]}
                                  >
                                    <Input placeholder='orders_customers' />
                                  </Form.Item>
                                  <Form.Item
                                    name='models'
                                    label='参与模型'
                                    rules={[{ required: true, message: '请选择模型' }]}
                                  >
                                    <Select mode='multiple' options={modelOptions} placeholder='选择两个或多个模型' />
                                  </Form.Item>
                                  <Form.Item name='type' label='关系类型'>
                                    <Select options={relationTypes.map(type => ({ label: type, value: type }))} />
                                  </Form.Item>
                                  <Form.Item
                                    name='condition'
                                    label='Join 条件'
                                    rules={[{ required: true, message: '请输入关联条件' }]}
                                  >
                                    <Input placeholder='orders.customer_id = customers.id' />
                                  </Form.Item>
                                  <Form.Item name='enabled' valuePropName='checked' label='启用'>
                                    <Switch />
                                  </Form.Item>
                                  <Button block icon={<LinkOutlined />} onClick={handleAddRelation}>
                                    添加关系
                                  </Button>
                                </Form>
                              </Card>
                            </Col>
                          </Row>
                        ),
                      },
                      {
                        key: 'knowledge',
                        label: '问数知识',
                        children: <MdlKnowledge database={selectedDatabase} models={models} />,
                      },
                      {
                        key: 'policies',
                        label: (
                          <span>
                            <SafetyCertificateOutlined /> 访问策略
                          </span>
                        ),
                        children: (
                          <Row gutter={[16, 16]}>
                            <Col xs={24} xl={16}>
                              <Card title='用户策略' size='small'>
                                <Table
                                  rowKey={record => record.id ?? record.user ?? JSON.stringify(record)}
                                  size='small'
                                  pagination={false}
                                  dataSource={policies}
                                  columns={policyColumns}
                                />
                              </Card>
                            </Col>
                            <Col xs={24} xl={8}>
                              <Card title='新增/编辑策略' size='small'>
                                <Form
                                  form={policyForm}
                                  disabled={!isAdmin}
                                  layout='vertical'
                                  initialValues={{ enabled: true }}
                                >
                                  <Form.Item
                                    name='user'
                                    label='用户'
                                    rules={[{ required: true, message: '请输入用户 ID' }]}
                                  >
                                    <Input placeholder='analyst@example.com' />
                                  </Form.Item>
                                  <Form.Item
                                    name='models'
                                    label='允许模型'
                                    rules={[{ required: true, message: '请选择模型' }]}
                                  >
                                    <Select mode='multiple' options={modelOptions} placeholder='选择允许访问的模型' />
                                  </Form.Item>
                                  {selectedPolicyModels.map(modelName => {
                                    const model = models.find(item => item.name === modelName);
                                    const options = normalizeColumns(model?.columns).map(column => ({
                                      label: column.name,
                                      value: column.name,
                                    }));
                                    return (
                                      <Form.Item
                                        key={modelName}
                                        name={['columnsByModel', modelName]}
                                        label={`${modelLabel(model ?? { name: modelName })} 字段`}
                                      >
                                        <Select
                                          mode='multiple'
                                          options={options}
                                          placeholder='默认允许该模型全部字段'
                                        />
                                      </Form.Item>
                                    );
                                  })}
                                  <Form.Item name='region_filter' label='区域值（region 等于）'>
                                    <Input placeholder='华东' />
                                  </Form.Item>
                                  <Form.Item name='enabled' valuePropName='checked' label='启用'>
                                    <Switch />
                                  </Form.Item>
                                  <Button block icon={<SafetyCertificateOutlined />} onClick={handleAddPolicy}>
                                    保存策略
                                  </Button>
                                </Form>
                              </Card>
                            </Col>
                          </Row>
                        ),
                      },
                    ]
                  : []),
                {
                  key: 'query',
                  label: (
                    <span>
                      <PlayCircleOutlined /> SQL 验证
                    </span>
                  ),
                  children: (
                    <Card size='small' title='通过 MDL 权限执行 SQL'>
                      <Space direction='vertical' className='w-full' size='middle'>
                        <Input.TextArea
                          value={sql}
                          onChange={event => setSql(event.target.value)}
                          rows={5}
                          placeholder='select * from orders limit 20'
                        />
                        <Space wrap>
                          <span className='text-sm text-gray-500'>Limit</span>
                          <InputNumber
                            min={1}
                            max={500}
                            value={sqlLimit}
                            onChange={value => setSqlLimit(value ?? 20)}
                          />
                          <Button
                            type='primary'
                            icon={<PlayCircleOutlined />}
                            loading={querying}
                            disabled={!canUseMdl}
                            onClick={handleQuery}
                          >
                            执行验证
                          </Button>
                          <Button>
                            <Link href={reactAgentMdlDemoUrl}>返回 React-agent 问数</Link>
                          </Button>
                        </Space>
                        {(queryResult.count !== undefined ||
                          queryResult.elapsed_ms !== undefined ||
                          queryResult.logical_sql) && (
                          <Space wrap className='text-sm text-gray-500'>
                            {queryResult.count !== undefined && <span>Rows: {queryResult.count}</span>}
                            {queryResult.elapsed_ms !== undefined && <span>Elapsed: {queryResult.elapsed_ms}ms</span>}
                            {queryResult.truncated !== undefined && (
                              <Tag color={queryResult.truncated ? 'orange' : 'green'}>
                                {queryResult.truncated ? 'truncated' : 'complete'}
                              </Tag>
                            )}
                            {queryResult.logical_sql && <Tag color='blue'>logical_sql</Tag>}
                          </Space>
                        )}
                        {queryResult.logical_sql && (
                          <pre className='max-h-32 overflow-auto rounded bg-gray-950 p-3 text-xs text-gray-100'>
                            {queryResult.logical_sql}
                          </pre>
                        )}
                        <Table
                          size='small'
                          pagination={{ pageSize: 20 }}
                          dataSource={queryResult.rows.map((row, rowIndex) => ({ key: rowIndex, row }))}
                          columns={queryResult.columns.map((column, columnIndex) => ({
                            title: column,
                            dataIndex: ['row', columnIndex],
                            ellipsis: true,
                          }))}
                          locale={{ emptyText: <Empty description='暂无查询结果' /> }}
                        />
                      </Space>
                    </Card>
                  ),
                },
                ...(isAdmin
                  ? [
                      {
                        key: 'raw',
                        label: 'JSON',
                        children: (
                          <Card
                            size='small'
                            title='当前 MDL JSON'
                            extra={
                              <Button
                                onClick={() => {
                                  setJsonText(JSON.stringify(currentManifest(), null, 2));
                                  setJsonOpen(true);
                                }}
                              >
                                编辑 JSON
                              </Button>
                            }
                          >
                            <pre className='max-h-[480px] overflow-auto rounded bg-gray-950 p-4 text-xs text-gray-100'>
                              {JSON.stringify(currentManifest(), null, 2)}
                            </pre>
                          </Card>
                        ),
                      },
                    ]
                  : []),
              ]}
            />

            {schemaTables.length > 0 && (
              <div className='mt-4 text-xs text-gray-400'>
                已从数据库 schema 读取 {schemaTables.length} 张表用于辅助建模。
              </div>
            )}
          </Spin>

          <Modal
            title='手动添加模型'
            open={addModelOpen}
            okText='添加到 MDL'
            cancelText='取消'
            confirmLoading={schemaLoading}
            onCancel={() => {
              setAddModelOpen(false);
              setAddModelTable(undefined);
            }}
            onOk={handleAddModel}
          >
            <Alert
              className='mb-4'
              type='info'
              showIcon
              message='从数据源的物理表创建一个可编辑的 MDL 模型'
              description='添加后可以在字段权限区域继续配置可见性、显示名称、业务描述、语义类型和计算表达式。'
            />
            <Form layout='vertical'>
              <Form.Item label='数据源' required>
                <Select
                  className='w-full'
                  value={selectedDatabase}
                  loading={schemaLoading}
                  options={dataSources.map(source => ({
                    label: source.name || source.db_name,
                    value: source.db_name || source.name,
                  }))}
                  onChange={switchDatabase}
                />
              </Form.Item>
              <Form.Item label='数据表' required>
                <Select
                  showSearch
                  className='w-full'
                  value={addModelTable}
                  loading={schemaLoading}
                  placeholder='选择要加入 MDL 的表'
                  optionFilterProp='label'
                  options={schemaTables.map(table => ({
                    label: table.name,
                    value: table.name,
                    disabled: models.some(model => model.name === table.name),
                  }))}
                  onChange={setAddModelTable}
                />
              </Form.Item>
            </Form>
          </Modal>

          <Modal
            title='编辑 MDL JSON'
            open={jsonOpen}
            width={860}
            onCancel={() => setJsonOpen(false)}
            onOk={() => {
              try {
                const nextManifest = JSON.parse(jsonText) as MdlDocument;
                const nextModels = (nextManifest.models ?? []).map(normalizeModel);
                setMdl({
                  ...emptyMdl,
                  ...mdl,
                  manifest: nextManifest,
                  models: nextModels,
                  relations: nextManifest.relationships ?? nextManifest.relations ?? [],
                });
                setSelection(buildSelection(nextModels));
                setSelectedModelName(nextModels[0]?.name);
                setJsonOpen(false);
              } catch {
                message.error('JSON 格式无效');
              }
            }}
          >
            <Input.TextArea value={jsonText} onChange={event => setJsonText(event.target.value)} rows={24} />
          </Modal>
        </>
      )}
    </div>
  );
}

export default MdlWorkbench;
