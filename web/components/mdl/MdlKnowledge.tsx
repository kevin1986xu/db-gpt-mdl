import {
  MdlKnowledgeCorpus,
  MdlKnowledgeEntry,
  MdlKnowledgeKind,
  MdlKnowledgeSearchMatch,
  MdlModel,
  approveMdlKnowledge,
  getMdlKnowledge,
  saveMdlKnowledge,
  searchMdlKnowledge,
} from '@/client/api/mdl';
import { apiInterceptors } from '@/client/api/tools/interceptors';
import {
  CheckCircleOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { Alert, Button, Card, Empty, Form, Input, Modal, Select, Space, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

type Props = {
  database?: string;
  models: MdlModel[];
};

type KnowledgeFormValues = {
  id?: string;
  kind: MdlKnowledgeKind;
  title: string;
  question?: string;
  content: string;
  sql?: string;
  aliasesText?: string;
  modelNames: string[];
  fieldsByModel?: Record<string, string[]>;
};

const emptyCorpus: MdlKnowledgeCorpus = { entries: [] };

function modelLabel(model: MdlModel) {
  return model.display_name || model.name;
}

function optionalModelLabel(modelName: string, model?: MdlModel) {
  return model ? modelLabel(model) : modelName;
}

function columnLabel(column: NonNullable<MdlModel['columns']>[number]) {
  return column.display_name ? `${column.display_name} (${column.name})` : column.name;
}

function splitAliases(value?: string) {
  return (value ?? '')
    .split(/[\n,，]/)
    .map(item => item.trim())
    .filter(Boolean);
}

function joinAliases(value?: string[]) {
  return (value ?? []).join('\n');
}

function normalizeEntries(entries?: MdlKnowledgeEntry[]) {
  return (entries ?? []).map(entry => ({
    ...entry,
    aliases: entry.aliases ?? [],
    models: entry.models ?? {},
    status: entry.status ?? 'draft',
  }));
}

function toSaveEntry(entry: MdlKnowledgeEntry): MdlKnowledgeEntry {
  const { stale: _stale, ...payload } = entry;
  return payload;
}

function createEntryId() {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function statusTag(entry: MdlKnowledgeEntry) {
  if (entry.stale) return <Tag color='orange'>需重新审核</Tag>;
  if (entry.status === 'approved') return <Tag color='green'>已审核</Tag>;
  return <Tag>草稿</Tag>;
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

function MdlKnowledge({ database, models }: Props) {
  const [corpus, setCorpus] = useState<MdlKnowledgeCorpus>(emptyCorpus);
  const [entries, setEntries] = useState<MdlKnowledgeEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [approvingId, setApprovingId] = useState<string>();
  const [dirty, setDirty] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string>();
  const [searchQuestion, setSearchQuestion] = useState('');
  const [searching, setSearching] = useState(false);
  const [matches, setMatches] = useState<MdlKnowledgeSearchMatch[]>([]);
  const [form] = Form.useForm<KnowledgeFormValues>();
  const loadId = useRef(0);
  const requestGeneration = useRef(0);
  const activeDatabase = useRef(database);
  const previousDatabase = useRef(database);
  activeDatabase.current = database;
  const selectedKind = Form.useWatch('kind', form);
  const selectedModelNames = ((Form.useWatch('modelNames', form) ?? []) as string[]).filter(Boolean);
  const hasLoadedVersion = typeof corpus.revision === 'number';
  const actionDisabled = !database || loading || saving || approvingId !== undefined || !hasLoadedVersion;
  const refreshDisabled = loading || saving || approvingId !== undefined;

  const modelOptions = useMemo(() => models.map(model => ({ label: modelLabel(model), value: model.name })), [models]);
  const modelMap = useMemo(
    () =>
      models.reduce<Record<string, MdlModel>>((acc, model) => {
        acc[model.name] = model;
        return acc;
      }, {}),
    [models],
  );

  useEffect(() => {
    activeDatabase.current = database;
    if (previousDatabase.current !== database) {
      requestGeneration.current += 1;
      previousDatabase.current = database;
      setCorpus(database ? { database, entries: [] } : emptyCorpus);
      setEntries([]);
      setDirty(false);
      setMatches([]);
      setSaving(false);
      setSearching(false);
      setApprovingId(undefined);
      setModalOpen(false);
      setEditingId(undefined);
    }
  }, [database]);

  const loadKnowledge = useCallback(async () => {
    const currentLoadId = ++loadId.current;
    const requestDatabase = database;
    if (!requestDatabase) {
      setCorpus(emptyCorpus);
      setEntries([]);
      setDirty(false);
      setMatches([]);
      return;
    }
    setLoading(true);
    try {
      const [error, result, response] = await apiInterceptors(getMdlKnowledge(requestDatabase));
      if (currentLoadId !== loadId.current || requestDatabase !== activeDatabase.current) return;
      if (error || !response?.success || !result) {
        return;
      }
      const nextEntries = normalizeEntries(result.entries);
      setCorpus({ ...result, entries: nextEntries });
      setEntries(nextEntries);
      setDirty(false);
      setMatches([]);
    } finally {
      if (currentLoadId === loadId.current) setLoading(false);
    }
  }, [database]);

  useEffect(() => {
    loadKnowledge();
  }, [loadKnowledge]);

  const currentKnowledge = useCallback(
    () => ({
      database,
      revision: corpus.revision,
      entries,
    }),
    [corpus.revision, database, entries],
  );

  const handleExportJson = () => {
    downloadJson(`${database || 'mdl'}-knowledge.json`, currentKnowledge());
  };

  const handleRefresh = () => {
    if (!dirty) {
      loadKnowledge();
      return;
    }
    Modal.confirm({
      title: '备份并刷新知识库',
      content:
        '当前有未保存的知识编辑。继续后会先下载一份本地 JSON 备份，然后加载服务器上的最新知识内容；这个操作不会自动覆盖服务器，也不会自动修改当前服务器版本。',
      okText: '备份并刷新',
      cancelText: '取消',
      onOk: () => {
        handleExportJson();
        loadKnowledge();
      },
    });
  };

  const saveEntries = async (nextEntries: MdlKnowledgeEntry[], generation: number) => {
    const requestDatabase = database;
    if (!requestDatabase) {
      message.warning('请先选择数据源');
      return undefined;
    }
    const [error, saved, response] = await apiInterceptors(
      saveMdlKnowledge({ database: requestDatabase, revision: corpus.revision, entries: nextEntries.map(toSaveEntry) }),
    );
    if (requestDatabase !== activeDatabase.current || generation !== requestGeneration.current) return undefined;
    if (error || !response?.success || !saved) {
      message.error('知识保存失败，当前编辑已保留，请核对版本后重试。');
      return undefined;
    }
    const normalized = normalizeEntries(saved.entries);
    setCorpus({ ...saved, entries: normalized });
    setEntries(normalized);
    setDirty(false);
    return { ...saved, entries: normalized };
  };

  const handleSaveAll = async () => {
    const requestDatabase = database;
    const generation = ++requestGeneration.current;
    setSaving(true);
    try {
      const saved = await saveEntries(entries, generation);
      if (saved && requestDatabase === activeDatabase.current && generation === requestGeneration.current) {
        setMatches([]);
        message.success('知识库已保存');
      }
    } finally {
      if (requestDatabase === activeDatabase.current && generation === requestGeneration.current) setSaving(false);
    }
  };

  const openCreate = () => {
    setEditingId(undefined);
    form.resetFields();
    form.setFieldsValue({ kind: 'qa_sql', modelNames: [] });
    setModalOpen(true);
  };

  const openEdit = (entry: MdlKnowledgeEntry) => {
    if (saving || approvingId !== undefined) return;
    setEditingId(entry.id);
    form.setFieldsValue({
      id: entry.id,
      kind: entry.kind,
      title: entry.title,
      question: entry.question,
      content: entry.content,
      sql: entry.sql,
      aliasesText: joinAliases(entry.aliases),
      modelNames: Object.keys(entry.models ?? {}),
      fieldsByModel: entry.models,
    });
    setModalOpen(true);
  };

  const handleDelete = (entry: MdlKnowledgeEntry) => {
    if (saving || approvingId !== undefined) return;
    Modal.confirm({
      title: '删除知识条目',
      content: `删除「${entry.title}」后需要点击保存才会写入后端。`,
      okText: '删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => {
        setEntries(prev => prev.filter(item => item.id !== entry.id));
        setDirty(true);
      },
    });
  };

  const handleSubmitEntry = async () => {
    const values = await form.validateFields();
    const bindings = values.modelNames.reduce<Record<string, string[]>>((acc, modelName) => {
      const fields = values.fieldsByModel?.[modelName] ?? [];
      if (fields.length > 0) acc[modelName] = fields;
      return acc;
    }, {});
    if (Object.keys(bindings).length === 0 || Object.keys(bindings).length !== values.modelNames.length) {
      message.warning('请为每个绑定模型至少选择一个字段');
      return;
    }
    const nextEntry: MdlKnowledgeEntry = {
      id: editingId ?? values.id ?? createEntryId(),
      kind: values.kind,
      title: values.title.trim(),
      question: values.kind === 'qa_sql' ? values.question?.trim() : values.question?.trim() || undefined,
      content: values.content.trim(),
      sql: values.kind === 'qa_sql' ? values.sql?.trim() : undefined,
      aliases: splitAliases(values.aliasesText),
      models: bindings,
      status: 'draft',
    };
    setEntries(prev => {
      const next = editingId ? prev.map(item => (item.id === editingId ? nextEntry : item)) : [...prev, nextEntry];
      return next;
    });
    setDirty(true);
    setModalOpen(false);
  };

  const handleApprove = async (entry: MdlKnowledgeEntry) => {
    const requestDatabase = database;
    const generation = ++requestGeneration.current;
    if (!requestDatabase) {
      message.warning('请先选择数据源');
      return;
    }
    setApprovingId(entry.id);
    try {
      const saved = await saveEntries(entries, generation);
      if (!saved) return;
      const [error, approved, response] = await apiInterceptors(
        approveMdlKnowledge({ database: requestDatabase, id: entry.id, revision: saved.revision }),
      );
      if (requestDatabase !== activeDatabase.current || generation !== requestGeneration.current) return;
      if (error || !response?.success || !approved) {
        message.error('审核失败，条目仍保留为草稿或需重新审核状态。');
        return;
      }
      const normalized = normalizeEntries(approved.entries);
      setCorpus({ ...approved, entries: normalized });
      setEntries(normalized);
      setDirty(false);
      setMatches([]);
      message.success('知识条目已审核');
    } finally {
      if (requestDatabase === activeDatabase.current && generation === requestGeneration.current)
        setApprovingId(undefined);
    }
  };

  const handleSearch = async () => {
    const requestDatabase = database;
    const generation = requestGeneration.current;
    if (!requestDatabase) {
      message.warning('请先选择数据源');
      return;
    }
    if (!searchQuestion.trim()) {
      message.warning('请输入测试问题');
      return;
    }
    setSearching(true);
    try {
      const [error, result, response] = await apiInterceptors(
        searchMdlKnowledge({ database: requestDatabase, question: searchQuestion.trim(), top_k: 4 }),
      );
      if (requestDatabase !== activeDatabase.current || generation !== requestGeneration.current) return;
      if (error || !response?.success || !result) return;
      setMatches(result.matches ?? []);
    } finally {
      if (requestDatabase === activeDatabase.current && generation === requestGeneration.current) setSearching(false);
    }
  };

  const columns: ColumnsType<MdlKnowledgeEntry> = [
    {
      title: '知识',
      dataIndex: 'title',
      render: (_, entry) => (
        <div>
          <div className='font-medium'>{entry.title}</div>
          <div className='text-xs text-gray-400'>
            {entry.kind === 'qa_sql' ? '问答 SQL' : '业务口径'}
            {entry.question ? ` · ${entry.question}` : ''}
          </div>
        </div>
      ),
    },
    {
      title: '状态',
      width: 110,
      render: (_, entry) => statusTag(entry),
    },
    {
      title: '绑定模型字段',
      render: (_, entry) => (
        <Space wrap size={[4, 4]}>
          {Object.entries(entry.models).map(([modelName, fields]) => (
            <Tag key={modelName}>
              {modelName}:{fields.length}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: '别名',
      dataIndex: 'aliases',
      render: value => (value?.length ? value.join(' / ') : '-'),
    },
    {
      title: 'SQL 示例',
      dataIndex: 'sql',
      ellipsis: true,
      render: value => value || '-',
    },
    {
      title: '操作',
      width: 210,
      render: (_, entry) => (
        <Space>
          <Button
            type='link'
            icon={<EditOutlined />}
            disabled={saving || approvingId !== undefined}
            onClick={() => openEdit(entry)}
          >
            编辑
          </Button>
          <Button
            type='link'
            icon={<CheckCircleOutlined />}
            loading={approvingId === entry.id}
            disabled={actionDisabled}
            onClick={() => handleApprove(entry)}
          >
            审核
          </Button>
          <Button
            danger
            type='link'
            icon={<DeleteOutlined />}
            disabled={saving || approvingId !== undefined}
            onClick={() => handleDelete(entry)}
          >
            删除
          </Button>
        </Space>
      ),
    },
  ];

  const matchColumns: ColumnsType<MdlKnowledgeSearchMatch> = [
    {
      title: '命中',
      dataIndex: 'title',
      render: (_, entry) => (
        <div>
          <div className='font-medium'>{entry.title}</div>
          <Typography.Paragraph className='!mb-0 text-xs text-gray-500' ellipsis={{ rows: 2 }}>
            {entry.content}
          </Typography.Paragraph>
        </div>
      ),
    },
    {
      title: 'Score',
      width: 90,
      render: (_, entry) => (typeof entry.score === 'number' ? entry.score.toFixed(3) : '-'),
    },
    {
      title: '状态',
      width: 110,
      render: (_, entry) => statusTag(entry),
    },
    {
      title: 'SQL',
      dataIndex: 'sql',
      ellipsis: true,
      render: value => value || '-',
    },
  ];

  return (
    <Space direction='vertical' className='w-full' size='middle'>
      <Alert
        type='info'
        showIcon
        message='知识条目用于提升问数命中率，不保存固定数值答案'
        description='请先保存 MDL 模型和字段，再审核知识条目。审核会绑定当前已保存 MDL 指纹；MDL 变化后，后端可将旧知识标记为需重新审核。'
      />
      <Card
        size='small'
        title={
          <Space>
            <span>知识条目</span>
            <Tag>版本 {corpus.revision ?? '未加载'}</Tag>
            {dirty && <Tag color='orange'>未保存</Tag>}
          </Space>
        }
        extra={
          <Space wrap>
            <Button icon={<DownloadOutlined />} disabled={!database || loading} onClick={handleExportJson}>
              导出知识 JSON
            </Button>
            <Button icon={<ReloadOutlined />} loading={loading} disabled={refreshDisabled} onClick={handleRefresh}>
              {dirty ? '备份并刷新' : '刷新'}
            </Button>
            <Button icon={<PlusOutlined />} type='primary' ghost disabled={actionDisabled} onClick={openCreate}>
              新增知识
            </Button>
            <Button
              type='primary'
              icon={<SaveOutlined />}
              loading={saving}
              disabled={actionDisabled || !dirty}
              onClick={handleSaveAll}
            >
              保存列表
            </Button>
          </Space>
        }
      >
        <Table
          rowKey='id'
          size='small'
          loading={loading}
          dataSource={entries}
          columns={columns}
          pagination={{ pageSize: 8 }}
          locale={{ emptyText: <Empty description={database ? '暂无知识条目' : '请先选择数据源'} /> }}
        />
      </Card>

      <Card size='small' title='搜索预览'>
        <Space.Compact className='w-full'>
          <Input
            value={searchQuestion}
            onChange={event => setSearchQuestion(event.target.value)}
            onPressEnter={handleSearch}
            placeholder='输入用户可能提出的问题，预览 top 4 知识命中'
          />
          <Button
            icon={<SearchOutlined />}
            loading={searching}
            disabled={!database || loading || saving || approvingId !== undefined}
            onClick={handleSearch}
          >
            搜索
          </Button>
        </Space.Compact>
        <Table
          className='mt-3'
          rowKey='id'
          size='small'
          pagination={false}
          dataSource={matches}
          columns={matchColumns}
          locale={{ emptyText: <Empty description='暂无检索结果' /> }}
        />
      </Card>

      <Modal
        title={editingId ? '编辑知识条目' : '新增知识条目'}
        open={modalOpen}
        width={820}
        okText='保存到本地列表'
        cancelText='取消'
        destroyOnClose
        onCancel={() => setModalOpen(false)}
        onOk={handleSubmitEntry}
      >
        <Form form={form} layout='vertical' initialValues={{ kind: 'qa_sql', modelNames: [] }}>
          <Form.Item name='kind' label='类型' rules={[{ required: true, message: '请选择类型' }]}>
            <Select
              options={[
                { label: '问答 SQL', value: 'qa_sql' },
                { label: '业务口径', value: 'business_rule' },
              ]}
            />
          </Form.Item>
          <Form.Item name='title' label='标题' rules={[{ required: true, message: '请输入标题' }]}>
            <Input placeholder='例如：活跃无人机数量口径' />
          </Form.Item>
          <Form.Item
            name='question'
            label='典型问题'
            rules={selectedKind === 'qa_sql' ? [{ required: true, message: '请输入典型问题' }] : undefined}
          >
            <Input placeholder='例如：当前有多少架无人机？' />
          </Form.Item>
          <Form.Item name='content' label='口径 / 内容' rules={[{ required: true, message: '请输入口径或内容' }]}>
            <Input.TextArea rows={4} placeholder='描述适用范围、过滤条件、指标口径和注意事项，不填写固定数值答案' />
          </Form.Item>
          <Form.Item
            name='sql'
            label='SQL 示例'
            rules={selectedKind === 'qa_sql' ? [{ required: true, message: '请输入 SQL 示例' }] : undefined}
          >
            <Input.TextArea rows={4} placeholder='select count(*) from device_registration ...' />
          </Form.Item>
          <Form.Item name='aliasesText' label='别名 / 近义词'>
            <Input.TextArea rows={2} placeholder='每行一个，或用逗号分隔' />
          </Form.Item>
          <Form.Item name='modelNames' label='绑定模型' rules={[{ required: true, message: '请选择绑定模型' }]}>
            <Select mode='multiple' options={modelOptions} placeholder='选择知识适用的 MDL 模型' />
          </Form.Item>
          {selectedModelNames.map(modelName => {
            const model = modelMap[modelName];
            const options = (model?.columns ?? []).map(column => ({
              label: columnLabel(column),
              value: column.name,
            }));
            return (
              <Form.Item
                key={modelName}
                name={['fieldsByModel', modelName]}
                label={`${optionalModelLabel(modelName, model)} 字段`}
              >
                <Select mode='multiple' options={options} placeholder='至少选择一个字段' />
              </Form.Item>
            );
          })}
        </Form>
      </Modal>
    </Space>
  );
}

export default MdlKnowledge;
