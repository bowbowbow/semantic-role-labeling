import fnmatch
import os
import re
from collections import defaultdict
# from itertools import izip

from srl.common.constants import INSTANCE_INDEX, LABEL_KEY, MARKER_KEY, SENTENCE_INDEX
from srl.common.srl_utils import read_json

START_OF_LABEL = "("
END_OF_LABEL = ")"
CONTINUATION = "*"
BEGIN = "B-"
IN = "I-"
SINGLE = "S-"
END = "E-"
OUT = "O"


class ConllReader(object):
    def __init__(self, index_field_map):
        super(ConllReader, self).__init__()
        self._index_field_map = index_field_map
        self.skip_line = lambda line: False

    def read_files(self, path, extension):
        if os.path.isdir(path):
            results = []
            for root, dir_names, file_names in os.walk(path):
                for file_name in fnmatch.filter(file_names, '*' + extension):
                    results.extend(self.read_file(os.path.join(root, file_name)))
            return results
        return self.read_file(path)

    def read_file(self, path):
        results = []
        with open(path) as conll_file:
            lines = []
            for line in conll_file:
                line = line.strip()
                if not line or self.skip_line(line):
                    if lines:
                        results.extend(self.read_instances([line.split() for line in lines]))
                        lines = []
                    continue
                lines.append(line)
            if lines:
                results.extend(self.read_instances([line.split() for line in lines]))
        return results

    def read_instances(self, rows):
        return [self.read_fields(rows)]

    def read_fields(self, rows):
        sentence = defaultdict(list)
        for row in rows:
            for index, val in self._index_field_map.items():
                sentence[val].append(row[index])
        return sentence


class ConllSrlReader(ConllReader):
    def __init__(self, index_field_map, pred_start, pred_end=0, pred_key="predicate"):
        super(ConllSrlReader, self).__init__(index_field_map)
        self._pred_start = pred_start
        self._pred_end = pred_end
        self._pred_index = [key for key, val in self._index_field_map.items() if val == pred_key][0]
        self.is_predicate = lambda x: x[self._pred_index] is not '-'
        self.prop_count = 0
        self.sentence_count = 0

    def read_instances(self, rows):
        instances = []
        fields = self.read_fields(rows)
        for key, labels in self.read_predicates(rows).items():
            instance = dict(fields)  # copy instance dictionary and add labels\
            instance[LABEL_KEY] = labels
            instance[MARKER_KEY] = [index == key and '1' or '0' for index in range(0, len(labels))]
            instance[INSTANCE_INDEX] = self.prop_count
            instance[SENTENCE_INDEX] = self.sentence_count
            instances.append(instance)
            self.prop_count += 1
        self.sentence_count += 1
        return instances

    def read_predicates(self, rows):
        pred_indices = []
        pred_cols = defaultdict(list)
        for token_idx, row_fields in enumerate(rows):
            if self.is_predicate(row_fields):
                pred_indices.append(token_idx)
            for index in range(self._pred_start, len(row_fields) - self._pred_end):
                pred_cols[index - self._pred_start].append(row_fields[index])
        # convert from CoNLL05 labels to IOB labels
        for key, val in pred_cols.items():
            # print('before _convert_to_iob: ', val)
            pred_cols[key] = ConllSrlReader._convert_to_iob(val)
            # print('after _convert_to_iob: ', pred_cols[key])

        assert len(pred_indices) <= len(pred_cols), (
                'Unexpected number of predicate columns: %d instead of %d'
                ', check that predicate start and end indices are correct: %s' % (len(pred_cols), len(pred_indices), rows))
        # create predicate dictionary with keys as predicate word indices and values as corr. lists of labels (1 for each token)
        predicates = {i: pred_cols[index] for index, i in enumerate(pred_indices)}

        # print('rows :', rows)
        # print('pred_indices :', pred_indices)
        # print('pred_cols :', pred_cols)
        # print('predicates :', predicates)
        return predicates

    @staticmethod
    def _convert_to_iob(labels):
        def _get_label(_label):
            return _label.replace(START_OF_LABEL, "").replace(END_OF_LABEL, "").replace(CONTINUATION, "")

        current = None
        results = []
        for token in labels:
            if token.startswith(START_OF_LABEL):
                label = _get_label(token)
                results.append(BEGIN + label)
                current = label
            elif current and CONTINUATION in token:
                results.append(IN + current)
            else:
                results.append(OUT)

            if token.endswith(END_OF_LABEL):
                current = None
        return results


class Conll2003Reader(ConllReader):
    def __init__(self, besio=False):
        super(Conll2003Reader, self).__init__({0: "word", 1: "pos", 2: "chunk", 3: "ne"})
        self.besio = besio

    def read_instances(self, rows):
        instances = super(Conll2003Reader, self).read_instances(rows)
        for instance in instances:
            instance[LABEL_KEY] = chunk(instance['ne'], besio=self.besio)
        return instances


class Conll2012NerReader(ConllReader):
    def __init__(self, besio=False):
        super(Conll2012NerReader, self).__init__({3: "word", 4: "pos", 5: "parse", 10: "ne"})
        self.besio = besio

    def read_instances(self, rows):
        instances = super(Conll2012NerReader, self).read_instances(rows)
        for instance in instances:
            instance[LABEL_KEY] = chunk(instance['ne'], besio=self.besio)
        return instances


class CustomSrlReader(ConllSrlReader):
    def __init__(self, index_field_map, pred_start):
        super(CustomSrlReader, self).__init__(index_field_map=index_field_map, pred_start=pred_start)

    @staticmethod
    def parse_json(json_file):
        fields = read_json(json_file)
        index_field_map = {val: key for (key, val) in fields['columns'].items()}
        pred_start = fields['arg_start_col']
        return {"index_field_map": index_field_map, "pred_start": pred_start}


class Conll2005Reader(ConllSrlReader):
    def __init__(self):
        super(Conll2005Reader, self).__init__({0: "word", 1: "pos", 2: "parse", 3: "ne", 4: "roleset", 5: "predicate"},
                                              pred_start=6)


class Conll2012Reader(ConllSrlReader):
    def __init__(self):
        super(Conll2012Reader, self).__init__({3: "word", 4: "pos", 5: "parse", 6: "predicate", 7: "roleset"},
                                              pred_start=11, pred_end=1)
        self.is_predicate = lambda x: x[self._pred_index] is not '-' and x[7] is not '-'
        self.skip_line = lambda line: line.startswith("#")  # skip comments


class ConllPhraseReader(Conll2005Reader):
    def __init__(self):
        super(ConllPhraseReader, self).__init__()

    def read_files(self, path, extension, phrase_path=None, phrase_ext=".chunks"):
        if os.path.isdir(path):
            if not phrase_path:
                phrase_path = path
            srl_files = [input_file for input_file in sorted(os.listdir(path)) if input_file.endswith(extension)]
            phrase_file = [re.sub(extension + "$", phrase_ext, srl_file) for srl_file in srl_files]
            results = []
            for srl_file, phrase_file in zip(srl_files, phrase_file):
                results.extend(self.read_file(os.path.join(path, srl_file), os.path.join(phrase_path, phrase_file)))
            return results
        return self.read_file(path, phrase_path, phrase_ext)

    def read_file(self, path, phrase_path=None, phrase_ext="chunks"):
        if not phrase_path:
            phrase_path = re.sub("\\..*?$", phrase_ext, path)
        results = []
        if not os.path.isfile(phrase_path):
            raise ValueError('Missing phrase file: {}'.format(phrase_path))
        with open(path) as conll_file, open(phrase_path) as phrase_file:
            lines, chunk_lines = [], []
            for line, chunk_line in zip(conll_file, phrase_file):
                line, chunk_line = line.strip(), chunk_line.strip()
                if (not line and chunk_line) or (not chunk_line and line):
                    raise ValueError(
                        'Misaligned phrase and CoNLL files: {} vs. {} in {} and {}'.format(chunk_line, line, phrase_path, path))
                if not line and lines:
                    results.extend(self.read_instances([line.split() for line in lines], phrases=chunk_lines))
                    lines, chunk_lines = [], []
                    continue
                lines.append(line)
                chunk_lines.append(chunk_line)
            if lines:
                results.extend(self.read_instances([line.split() for line in lines], phrases=chunk_lines))
        return results

    def read_instances(self, rows, phrases=None):
        if not phrases:
            raise ValueError("Phrases not provided for instance: {}".format(rows))
        instances = []
        for index, labels in self.read_predicates(rows).items():
            instance = self._read_chunks(rows, phrase_labels=phrases, predicate_index=index, labels=labels)
            instance[INSTANCE_INDEX] = self.prop_count
            instance[SENTENCE_INDEX] = self.sentence_count
            instances.append(instance)
            self.prop_count += 1
        self.sentence_count += 1
        return instances

    def _read_chunks(self, rows, phrase_labels, predicate_index, labels):
        new_labels = []  # label per phrase
        predicate_chunk_index = -1  # index of phrase containing the predicate
        phrases = []  # list of phrases, each phrase represented by a list of fields from the input file
        curr_chunk = []  # the phrase currently being updated
        prev_label = None
        assert len(rows) == len(phrase_labels) == len(labels), 'Unequal number of rows phrases, and labels: {} vs. {} vs. {}' \
            .format(len(rows), len(phrase_labels), len(labels))

        for token_index, (row, curr_label) in enumerate(zip(rows, phrase_labels)):
            if _end_of_chunk(prev_label, curr_label):
                phrases.append(curr_chunk)
                curr_chunk = []
            elif curr_chunk:
                curr_chunk.append(row)

            if _start_of_chunk(prev_label, curr_label):
                curr_chunk.append(row)
            if predicate_index == token_index:
                predicate_chunk_index = len(phrases)
            prev_label = curr_label
        if curr_chunk:
            phrases.append(curr_chunk)

        word_index = 0
        new_index = -1
        fixed_phrases = []
        for index, phrase in enumerate(phrases):
            if index == predicate_chunk_index:
                for row in phrase:
                    if word_index == predicate_index:
                        new_index = len(fixed_phrases)
                    fixed_phrases.append([row])
                    new_labels.append(labels[word_index])
                    word_index += 1
            else:
                fixed_phrases.append(phrase)
                new_labels.append(labels[word_index])
                word_index += len(phrase)

        instance = defaultdict(list)
        for phrase in [self.read_fields(phrase) for phrase in fixed_phrases]:
            for key, val in phrase.items():
                instance[key].append(val)
        instance[LABEL_KEY] = new_labels
        instance[MARKER_KEY] = [index == new_index and '1' or '0' for index in range(0, len(new_labels))]
        return instance


def _end_of_chunk(prev, curr):
    prev_val, prev_tag = _get_val_and_tag(prev)
    curr_val, curr_tag = _get_val_and_tag(curr)
    if prev_val == OUT:
        return True
    if not prev_val:
        return False
    if prev_tag != curr_tag or prev_val == 'E' or curr_val == 'B' or curr_val == 'O' or prev_val == 'O':
        return True
    return False


def _start_of_chunk(prev, curr):
    prev_val, prev_tag = _get_val_and_tag(prev)
    curr_val, curr_tag = _get_val_and_tag(curr)
    if prev_tag != curr_tag or curr_val == 'B' or curr_val == 'O':
        return True
    return False


def _get_val_and_tag(label):
    if not label:
        return '', ''
    if label == 'O':
        return label, ''
    return label.split('-')


def chunk(labeling, besio=True, conll=False):
    if conll:
        besio = True
    result = []
    prev_type = None
    curr = []
    for label in labeling:
        if label == 'O' or label == '<UNK>':
            state, chunk_type = 'O', ''
        else:
            split_index = label.index('-')
            state, chunk_type = label[:split_index], label[split_index + 1:]
        if state == 'I' and chunk_type != prev_type:  # new chunk of different type
            state = 'B'
        if state in 'OB' and curr:  # end of chunk
            result += _to_besio(curr) if besio else curr
            curr = []
        if state == 'O':
            result.append(state)
        else:
            curr.append(state + "-" + chunk_type)
        prev_type = chunk_type
    if curr:
        result += _to_besio(curr) if besio else curr
    if conll:
        result = [_to_conll(label) for label in result]
    return result


def _to_besio(iob_labeling):
    if len(iob_labeling) == 1:
        return ['S' + iob_labeling[0][1:]]
    return iob_labeling[:-1] + ['E' + iob_labeling[-1][1:]]


def _to_conll(iob_label):
    label_type = iob_label.replace(BEGIN, "").replace(END, "").replace(SINGLE, "").replace(IN, "")
    if iob_label.startswith(BEGIN):
        return "(" + label_type + "*"
    if iob_label.startswith(SINGLE):
        return "(" + label_type + "*)"
    if iob_label.startswith(END):
        return "*)"
    return "*"
