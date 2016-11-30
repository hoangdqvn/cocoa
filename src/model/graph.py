from collections import defaultdict
import numpy as np
from itertools import izip, islice
from src.model.vocab import is_entity, Vocabulary
from src.model.graph_embedder import GraphEmbedderConfig

def add_graph_arguments(parser):
    parser.add_argument('--num-items', type=int, default=10, help='Number of items in each KB')
    parser.add_argument('--entity-hist-len', type=int, default=10, help='Number of past words to search for entities')
    parser.add_argument('--max-num-entities', type=int, default=30, help='Estimate of maximum number of entities in a dialogue')
    parser.add_argument('--max-degree', type=int, default=10, help='Maximum degree of a node in the graph')

def inv_rel(relation):
    return '*' + relation

def item_to_str(id_):
    return 'item-%d' % id_

class GraphMetadata(object):
    '''
    Schema information and basic config of Graph.
    '''
    def __init__(self, schema, entity_map, relation_map, utterance_size, max_num_entities, max_degree=10, entity_hist_len=10, entity_cache_size=2, num_items=10):
        # {attribute_name: attribute_type}, e.g., 'Name': 'person'
        self.attribute_types = schema.get_attributes()

        # Entity to id
        self.entity_map = entity_map

        # Relation to id. Add inverse relations.
        self.relation_map = relation_map

        # An utterance udpate all entities within entity_hist_len (counting backward
        # from the current position)
        self.entity_hist_len = entity_hist_len
        # Maximum number of nodes/entities to update embeddings for. NOTE: this should be
        # the same as what in GraphEmbedderConfig.
        self.entity_cache_size = entity_cache_size

        # Maximum number of entites that may appear in one dialogue. This affects the
        # initial utterance matrix size.
        self.max_num_entities = max_num_entities

        # Node features {feat_name: (offset, feat_size)}
        # degree: 0-max_degree
        # node_type: entity, item, attr
        degree_size = max_degree + 1
        node_types = Vocabulary(unk=False)
        # Entity types, e.g. major, school
        node_types.add_words(self.attribute_types.values())
        # Attribute names, e.g. Name, Company
        #node_types.add_words([x.lower() for x in self.attribute_types.keys()])
        # Item names/ids
        #node_types.add_words([item_to_str(i) for i in xrange(num_items)])
        node_types.add_words(['item', 'attr'])
        self.feat_inds = {'degree': (0, degree_size), 'node_type': (degree_size, node_types.size)}
        self.feat_size = sum([v[1] for v in self.feat_inds.values()])
        self.node_types = node_types

        # This affects the size of the utterance matrix
        self.utterance_size = utterance_size

        # Padding
        # NOTE: *_PAD are overlapping with actual nodes and edges.
        # But this is fine as padded node embeddings will be masked in get_context thus there's
        # no gradient propogated back to the actual nodes and edges.
        self.NODE_PAD = 0
        self.EDGE_PAD = 0
        self.ENTITY_PAD = 0
        self.PATH_PAD = [self.NODE_PAD, self.EDGE_PAD, self.NODE_PAD]
        self.PAD_PATH_ID = 0

class GraphBatch(object):
    def __init__(self, graphs):
        self.graphs = graphs
        self.batch_size = len(graphs)

    def _max_num_nodes(self):
        return max([graph.nodes.size for graph in self.graphs])

    def _max_num_paths(self):
        return max([graph.paths.shape[0] for graph in self.graphs])

    def _max_num_paths_per_node(self):
        return max([max([paths.shape[0] for paths in graph.node_paths]) for graph in self.graphs])

    def _make_batch(self, shape, fill_value, dtype, attr):
        batch_data = np.full(shape, fill_value, dtype=dtype)
        for i, graph in enumerate(self.graphs):
            data = getattr(graph, attr)
            batch_data[i][:data.shape[0]] = data
        return batch_data

    def _batch_node_ids(self, max_num_nodes):
        return self._make_batch((self.batch_size, max_num_nodes), Graph.metadata.NODE_PAD, np.int32, 'node_ids')

    def _batch_mask(self, max_num_nodes):
        mask = np.full((self.batch_size, max_num_nodes), False, dtype=np.bool)
        for i, graph in enumerate(self.graphs):
            mask[i][:len(graph.node_ids)] = True
        return mask

    def _batch_entity_ids(self, max_num_nodes):
        return self._make_batch((self.batch_size, max_num_nodes), Graph.metadata.ENTITY_PAD, np.int32, 'entity_ids')

    def _batch_paths(self, max_num_paths):
        return self._make_batch((self.batch_size, max_num_paths, 3), 0, np.int32, 'paths')

    def _batch_node_paths(self, max_num_nodes, max_num_paths_per_node):
        batch_data = np.full((self.batch_size, max_num_nodes, max_num_paths_per_node), Graph.metadata.PAD_PATH_ID, dtype=np.int32)
        for i, graph in enumerate(self.graphs):
            for j, node_path in enumerate(graph.node_paths):
                batch_data[i][j][:len(node_path)] = node_path
        return batch_data

    def _batch_node_feats(self, max_num_nodes):
        return self._make_batch((self.batch_size, max_num_nodes, Graph.metadata.feat_size), 0, np.float32, 'feats')

    def update_entities(self, tokens):
        assert len(tokens) == self.batch_size
        for graph, toks in izip(self.graphs, tokens):
            # toks is None when this is a padded turn
            if toks is not None:
                graph.read_utterance(toks)

    def _batch_entity_lists(self, entity_lists, pad_utterance_id):
        max_len = Graph.metadata.entity_cache_size
        batch_entity_lists = np.full([self.batch_size, max_len], pad_utterance_id, dtype=np.int32)
        for i, entity_list in enumerate(entity_lists):
            n = len(entity_list)
            if n == 0:
                continue
            elif n > max_len:
                # Take the most recent ones
                batch_entity_lists[i] = entity_list[-1*max_len:]
            else:
                batch_entity_lists[i][:n] = entity_list
        return batch_entity_lists

    def copy_targets(self, targets, vocab_size):
        '''
        Replace targets that are entities to node ids, so that we learn to copy them from graph.
        We assume that entities in targets are mapped by entity_map and offset by vocab.size.
        '''
        new_targets = np.array(targets)
        for i, graph in enumerate(self.graphs):
            for j, t in enumerate(new_targets[i]):
                if t >= vocab_size:
                    entity = Graph.metadata.entity_map.to_word(t - vocab_size)
                    new_targets[i][j] = graph.nodes.to_ind(entity) + vocab_size
        return new_targets

    def copy_preds(self, preds, vocab_size):
        '''
        Inverse of copy_targets.
        '''
        new_preds = np.array(preds)
        for i, graph in enumerate(self.graphs):
            for j, t in enumerate(new_preds[i]):
                if t >= vocab_size:
                    try:
                        entity = graph.nodes.to_word(t - vocab_size)
                    except KeyError:
                        new_preds[i][j] = 0  # <unk>
                        continue
                    new_preds[i][j] = Graph.metadata.entity_map.to_ind(entity) + vocab_size
        return new_preds

    def update_graph(self, tokens):
        '''
        Update graph: add new entities tokens.
        Return lists of entities at the end of the sequence of tokens so that the encoder
        or decoder can update the utterance matrix accordingly.
        '''
        if tokens is not None:
            self.update_entities(tokens)
        entity_lists = [graph.get_entity_list(1)[0] for graph in self.graphs]
        return entity_lists

    def _batch_zero_utterances(self, max_num_nodes):
        # Plus one because the last utterance is the padding.
        num_rows = max(max_num_nodes, Graph.metadata.max_num_entities) + 1
        self.pad_utterance_id = num_rows - 1
        return np.zeros([self.batch_size, num_rows, Graph.metadata.utterance_size], dtype=np.float32)

    def update_utterances(self, utterances, max_num_nodes):
        return (self._update_utterances(utterances[0], max_num_nodes),
                self._update_utterances(utterances[1], max_num_nodes))

    def _update_utterances(self, utterances, max_num_nodes):
        '''
        Resize utterance matrix if there are more nodes (expensive).
        '''
        num_rows = utterances.shape[1]
        if num_rows > max_num_nodes:
            return utterances
        else:
            new_utterances = self._batch_zero_utterances(max_num_nodes)
            old_num_rows = num_rows
            batch_size, num_rows, _ = new_utterances.shape
            for i in xrange(batch_size):
                new_utterances[i][:old_num_rows] = utterances[i]
            return new_utterances

    def get_zero_checklists(self, seq_len):
        max_num_nodes = self._max_num_nodes()
        return np.zeros([self.batch_size, seq_len, max_num_nodes])

    def get_zero_copied_nodes(self, seq_len):
        return np.zeros([self.batch_size, seq_len], dtype=np.int32), np.zeros([self.batch_size, seq_len], dtype=np.bool)

    def update_copied_nodes(self, targets, entities, mask, vocab):
        for i in xrange(self.batch_size):
            entity_id = self.graphs[i].output_to_node_id(targets[i], vocab)
            if entity_id is not None:
                entities[i] = entity_id
                mask[i] = True

    def get_copied_nodes(self, targets, vocab):
        '''
        Return the node id of the predictions/targets if the output is copied from the context,
        mask=False means the output is from the vocab.
        '''
        batch_size, seq_len = targets.shape
        copied_nodes, mask = self.get_zero_copied_nodes(seq_len)
        for i in xrange(1, seq_len):
            self.update_copied_nodes(targets[:, i-1], copied_nodes[:, i], mask[:, i], vocab)
        return copied_nodes, mask

    def get_checklists(self, targets, vocab, init_cl=None):
        '''
        Return checklists for a batch of sequences. (batch_size, seq_len, num_nodes)
        targets: (batch_size, seq_len)
        init_cl: the initial checklist from previous sequences
        (batch_size, num_nodes)
        '''
        batch_size, seq_len = targets.shape
        assert batch_size == self.batch_size
        cl = self.get_zero_checklists(seq_len)
        if init_cl is not None:
            init_num_nodes = init_cl.shape[2]
            # We might have more nodes now
            cl[:, 0, :init_num_nodes] = init_cl
        for i in xrange(1, seq_len):
            cl[:, i, :] = cl[:, i-1, :]
            self.update_checklist(targets[:, [i-1]], cl[:, i, :], vocab)
        return cl

    def update_checklist(self, outputs, cl, vocab):
        '''
        Mark mentioned entities in outputs in a checklist.
        outputs: integers. words are mapped by vocab and entities are mapped by entity_map
        offset by vocab.size.
        (batch_size, 1)
        cl: checklist to be updated in place.
        (batch_size, num_nodes)
        '''
        for i in xrange(self.batch_size):
            output = outputs[i][0]
            entity_id = self.graphs[i].output_to_node_id(output, vocab)
            if entity_id is not None:
                cl[i][entity_id] = 1

    def get_batch_data(self, encoder_tokens, decoder_tokens, utterances):
        '''
        Construct batched inputs for GraphEmbedder. (These could be precomputed as well but
        can take lots of memory.)
        - Extract entities from encoder_tokens, decoder_tokens to update their utterances.
        - At the beginning of a dialogue, provide zero utterance matrices; during the dialogue
          we will get updated utterance matrices from GraphEmbedder.
        - node_ids, entity_ids, paths, node_paths, node_feats
        '''
        encoder_entity_lists = self.update_graph(encoder_tokens)
        decoder_entity_lists = self.update_graph(decoder_tokens)

        max_num_nodes = self._max_num_nodes()
        if utterances is None:
            # Encoder utterances and decoder utterances
            utterances = (self._batch_zero_utterances(max_num_nodes),
                          self._batch_zero_utterances(max_num_nodes))
        else:
            utterances = self.update_utterances(utterances, max_num_nodes)

        max_num_paths = self._max_num_paths()
        max_num_paths_per_node = self._max_num_paths_per_node()
        batch = {
                 'node_ids': self._batch_node_ids(max_num_nodes),
                 'mask': self._batch_mask(max_num_nodes),
                 'entity_ids': self._batch_entity_ids(max_num_nodes),
                 'paths': self._batch_paths(max_num_paths),
                 'node_paths': self._batch_node_paths(max_num_nodes, max_num_paths_per_node),
                 'node_feats': self._batch_node_feats(max_num_nodes),
                 'utterances': utterances,
                 'encoder_entities': self._batch_entity_lists(encoder_entity_lists, self.pad_utterance_id),
                 'decoder_entities': self._batch_entity_lists(decoder_entity_lists, self.pad_utterance_id)
                }
        return batch

class Graph(object):
    '''
    Maintain a (dynamic) knowledge graph of the agent.
    '''
    metadata = None

    def __init__(self, kb):
        assert Graph.metadata is not None
        self.kb = kb
        self.reset()

    def reset(self):
        '''
        Clear all information from dialogue history and only keep KB information.
        This is required during training when we go through one dialogue multiple times.
        '''
        # Map each node in the graph to an integer
        self.nodes = Vocabulary(unk=False)
        # All paths in the KB; each path is a 3-tuple (node_id, edge_id, node_id)
        # NOTE: The first path is always a padding path
        self.paths = [Graph.metadata.PATH_PAD]
        # Read information form KB to fill in nodes and paths
        self.load_kb(self.kb)

        # Input data to feed_dict
        self.node_ids = np.arange(self.nodes.size, dtype=np.int32)
        self.entity_ids = np.array([Graph.metadata.entity_map.to_ind(self.nodes.to_word(i)) for i in xrange(self.nodes.size)], dtype=np.int32)
        self.paths = np.array(self.paths, dtype=np.int32)
        self.feats = self.get_features()
        self.node_paths = self.get_node_paths()

        # Entity/token sequence in the dialogue
        self.entities = []

    def get_node_paths(self):
        node_paths = []
        for node_id in self.node_ids:
            # Skip the first padding path
            paths = [path_id for path_id, path in enumerate(self.paths) if path_id != Graph.metadata.PAD_PATH_ID and path[0] == node_id]
            node_paths.append(np.array(paths, dtype=np.int32))
        return node_paths

    def get_input_data(self):
        '''
        Return feed_dict data to the GraphEmbed model.
        '''
        assert self.node_ids.shape[0] == self.feats.shape[0]
        return (self.node_ids, self.entity_ids, self.paths, self.feats)

    def _add_path(self, node1, relation, node2):
        node1_id = self.nodes.to_ind(node1)
        node2_id = self.nodes.to_ind(node2)
        rel = Graph.metadata.relation_map.to_ind(relation)
        irel = Graph.metadata.relation_map.to_ind(inv_rel(relation))
        self.paths.append((node1_id, rel, node2_id))
        self.paths.append((node2_id, irel, node1_id))

    def load_kb(self, kb):
        '''
        Construct 3 types of nodes: item, entity, attribute
        and 2 types of paths: (item, has_attr, entity) and (attr has entity)
        '''
        attr_ents = defaultdict(set)  # Entities of each attribute
        for i, item in enumerate(kb.items):
            # Item nodes
            item_node = (item_to_str(i), 'item')
            #item_name = item_to_str(i)
            #item_node = (item_name, item_name)
            self.nodes.add_word(item_node)
            attrs = sorted(item.items(), key=lambda x: x[0])
            for attr_name, value in attrs:
                type_ = Graph.metadata.attribute_types[attr_name]
                attr_name = attr_name.lower()
                value = value.lower()
                # Attribute nodes
                attr_node = (attr_name, 'attr')
                #attr_node = (attr_name, attr_name)
                self.nodes.add_word(attr_node)
                # Entity nodes
                entity_node = (value, type_)
                self.nodes.add_word(entity_node)
                # Path: item has_attr entity
                self._add_path(item_node, attr_name, entity_node)
                attr_ents[attr_node].add(entity_node)
        # Path: attr has entity
        for attr_node, ent_set in attr_ents.iteritems():
            for entity_node in ent_set:
                self._add_path(attr_node, 'has', entity_node)
        self.paths = np.array(self.paths, dtype=np.int32)

    def read_utterance(self, tokens):
        '''
        Map entities to node ids and tokens to -1. Add new nodes if needed.
        tokens: from batch['encoder/decoder_tokens']; entities are represented
        as (surface_form, (canonical_form, type)), i.e. output of entitylink.
        '''
        new_entities = set([x[1] for x in tokens if is_entity(x) and not self.nodes.has(x[1])])
        if len(new_entities) > 0:
            self.add_entity_nodes(new_entities)
        node_ids = (self.nodes.to_ind(x[1]) if is_entity(x) else -1 for x in tokens)
        self.entities.extend(node_ids)

    def _update_nodes(self, entities):
        self.nodes.add_words(entities)
        self.node_ids = np.arange(self.nodes.size, dtype=np.int32)

    def _update_feats(self, entities):
        # degree=0, node_type=entity type
        feats = [[0, self._node_type(x)] for x in entities]
        new_feat_vec = self.get_feat_vec(feats)
        self.feats = np.concatenate((self.feats, new_feat_vec), axis=0)

    def _update_entity_ids(self, entities):
        self.entity_ids = np.concatenate([self.entity_ids,
                   [Graph.metadata.entity_map.to_ind(entity) for entity in entities]], axis=0)

    def _update_node_paths(self, entities):
        '''
        New entities map to the padded path.
        '''
        for _ in entities:
            self.node_paths.append(np.array([Graph.metadata.PAD_PATH_ID]))

    def add_entity_nodes(self, entities):
        # Paths do not change, no need to update
        self._update_nodes(entities)
        self._update_entity_ids(entities)
        self._update_feats(entities)
        self._update_node_paths(entities)

    def get_entity_list(self, last_n=None):
        '''
        Input: return entity_list for the n most recent tokens received
        Output: a list of entity list at each position of received entities
        - E.g. I went to Stanford and MIT . => [[], [], [], [Stanford], [Stanford], [Stanford, MIT]]
        '''
        N = len(self.entities)
        if N == 0:
            if not last_n:
                return [[]]
            return [[] for _ in xrange(last_n)]
        if not last_n:
            position = xrange(N)
        else:
            assert last_n <= N
            position = (N - i for i in xrange(last_n, 0, -1))
        entity_list = [self.get_entities(max(0, i-Graph.metadata.entity_hist_len), i+1) for i in position]
        return entity_list

    def get_entities(self, start, end):
        '''
        Return all entity ids (from self.nodes) in [start, end).
        '''
        # Filter tokens and remove duplicated entities
        seen = set()
        entities = [entity for entity in islice(self.entities, start, end) if \
                entity != -1 and not (entity in seen or seen.add(entity))]
        return entities

    def _node_type(self, node):
        # Use fine categorty for item and attr nodes
        name, type_ = node
        #return name if type_ == 'item' or type_ == 'attr' else type_
        return type_

    def get_features(self):
        feats = [[0, self._node_type(self.nodes.to_word(i))] for i in xrange(self.nodes.size)]
        # Compute degree of each node
        for path in self.paths:
            n1, r, n2 = path
            feats[n1][0] += 1
        return self.get_feat_vec(feats)

    def get_feat_vec(self, raw_feats):
        '''
        Input: a list of features [degree, node_type] for each node
        Output: one-hot encoded numpy feature matrix
        '''
        f = np.zeros([len(raw_feats), Graph.metadata.feat_size])

        def get_index(feat_name, feat_value):
            offset, size = Graph.metadata.feat_inds[feat_name]
            assert feat_value < size
            return offset + feat_value

        for i, (degree, node_type) in enumerate(raw_feats):
            f[i][get_index('degree', degree)] = 1
            f[i][get_index('node_type', Graph.metadata.node_types.to_ind(node_type))] = 1

        return f

    def output_to_node_id(self, output, vocab):
        '''
        Map output prediction/target to local node ids.
        '''
        entity = None
        if output >= vocab.size:
            entity = Graph.metadata.entity_map.to_word(output - vocab.size)
        else:
            word = vocab.to_word(output)
            if is_entity(word):
                entity = word
        if entity is not None:
            try:
                return self.nodes.to_ind(entity)
            except KeyError:
                # If the entity is from vocab, it may not be in the nodes of the graph
                pass
        return None