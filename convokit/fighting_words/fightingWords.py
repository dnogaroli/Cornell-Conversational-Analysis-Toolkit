import numpy as np
from sklearn.feature_extraction.text import CountVectorizer as CV
import string
from convokit import Transformer
from convokit.model import Corpus, Utterance
from typing import List, Callable, Tuple
from matplotlib import pyplot as plt
import pandas as pd

exclude = set(string.punctuation)

class FightingWords(Transformer):
    """
    Based on Monroe et al.'s "Fightin’ Words: Lexical Feature Selection and Evaluation for Identifying the Content of Political Conflict"

    Implementation adapted from Jack Hessel's https://github.com/jmhessel/FightingWords
    
    - ngram; an int describing up to what n gram you want to consider (1 is unigrams,
        2 is bigrams + unigrams, etc). Ignored if a custom CountVectorizer is passed.
    - prior; either a float describing a uniform prior, or a vector describing a prior
        over vocabulary items. If you're using a predefined vocabulary, make sure to specify that
        when you make your CountVectorizer object.
    - threshold: the z-score threshold for annotating utterances with identified ngrams
    - top_k: use the top k ngrams when annotating utterances
    - annot_method: "top_k" or "threshold" to specify which annotation method to use
    - cv; a sklearn.feature_extraction.text.CountVectorizer object, if desired.
    """
    def __init__(self, class1_selector: Callable[[Utterance], bool],
                 class2_selector: Callable[[Utterance], bool], cv=None,
                 ngram=None, prior=0.1, threshold=1, top_k=10, annot_method="top_k",
                 string_sanitizer=lambda str_: FightingWords._basic_sanitize(str_)):
        self.class1_selector = class1_selector
        self.class2_selector = class2_selector
        self.ngram = ngram
        self.prior = prior
        self.cv = cv
        self.threshold = threshold
        self.top_k = top_k
        assert annot_method in ["top_k", "threshold"]
        self.annot_method = annot_method
        self.ngram_zscores = None
        self.string_sanitizer = string_sanitizer
        self._count_matrix = None
        if self.cv is None and type(self.prior) is not float:
            raise ValueError("If using a non-uniform prior, you must pass a count vectorizer with "
                             "the vocabulary parameter set.")
        if self.cv is None:
            print("Initializing default CountVectorizer...")
            if self.ngram is None:
                self.ngram = (1, 3)
            self.cv = CV(decode_error='ignore', min_df=10, max_df=.5, ngram_range=self.ngram,
                    binary=False,
                    max_features=15000)
    @staticmethod
    def _basic_sanitize(in_string):
        '''Returns a very roughly sanitized version of the input string.'''
        return_string = (b' '.join(in_string.encode('ascii', 'ignore').strip().split())).decode('ascii')
        return_string = ''.join(ch for ch in return_string if ch not in exclude)
        return_string = return_string.lower()
        return_string = ' '.join(return_string.split())
        return return_string

    def _bayes_compare_language(self, class1: List[Utterance], class2: List[Utterance]):
        '''
        Arguments:
        - class1, class2; a list of strings from each language sample

        Returns:
        - A dict of length |Vocab| with (n-gram, zscore) pairs.'''
        class1 = [self.string_sanitizer(utt.text) for utt in class1]
        class2 = [self.string_sanitizer(utt.text) for utt in class2]

        counts_mat = self.cv.fit_transform(class1+class2).toarray()
        # Now sum over languages...
        vocab_size = len(self.cv.vocabulary_)
        print("Vocab size is {}".format(vocab_size))
        if type(self.prior) is float:
            priors = np.array([self.prior for _ in range(vocab_size)])
        else:
            priors = self.prior
        z_scores = np.empty(priors.shape[0])
        count_matrix = np.empty([2, vocab_size], dtype=np.float32)
        count_matrix[0, :] = np.sum(counts_mat[:len(class1), :], axis=0)
        count_matrix[1, :] = np.sum(counts_mat[len(class1):, :], axis=0)
        self._count_matrix = count_matrix
        a0 = np.sum(priors)
        n1 = 1.*np.sum(count_matrix[0, :])
        n2 = 1.*np.sum(count_matrix[1, :])
        print("Comparing language...")
        for i in range(vocab_size):
            #compute delta
            term1 = np.log((count_matrix[0, i] + priors[i])/(n1 + a0 - count_matrix[0, i] - priors[i]))
            term2 = np.log((count_matrix[1, i] + priors[i])/(n2 + a0 - count_matrix[1, i] - priors[i]))
            delta = term1 - term2
            #compute variance on delta
            var = 1./(count_matrix[0, i] + priors[i]) + 1./(count_matrix[1, i] + priors[i])
            #store final score
            z_scores[i] = delta/np.sqrt(var)
        index_to_term = {v: k for k, v in self.cv.vocabulary_.items()}
        sorted_indices = np.argsort(z_scores)
        return {index_to_term[i]: z_scores[i] for i in sorted_indices}

    def fit(self, corpus: Corpus, y=None):
        class1, class2 = [], []
        for utt in corpus.iter_utterances():
            if self.class1_selector(utt):
                class1.append(utt)
            elif self.class2_selector(utt):
                class2.append(utt)

        if len(class1) == 0:
            raise ValueError("class1_func returned 0 valid utterances.")
        if len(class2) == 0:
            raise ValueError("class2_func returned 0 valid utterances.")

        print("class1_func returned {} valid utterances. class2_func returned {} valid utterances.".format(len(class1), len(class2)))

        self.ngram_zscores = self._bayes_compare_language(class1, class2)
        print("ngram zscores computed.")

    def get_ngram_zscores(self):
        """
        :return: a DataFrame of ngrams with zscores and classes, indexed by the ngrams
        """

        if self.ngram_zscores is None:
            raise ValueError("fit() must be run on a corpus first.")
        df = pd.DataFrame(list(self.ngram_zscores.items()), columns=['ngram', 'z-score']).set_index('ngram')
        df['class'] = (df['z-score'] >= 0).apply(lambda x: ["class2", "class1"][int(x)])
        return df

    def get_top_k_ngrams(self, top_k=None) -> Tuple[List[str], List[str]]:
        """
        Returns the (ordered) top k ngrams for both classes
        :param top_k: if left as default None, FightingWords.top_k will be used.
        :return: two ordered lists of ngrams (with descending z-score):
                first list is for class 1, second list is for class 2
        """
        if self.ngram_zscores is None:
            raise ValueError("fit() must be run on a corpus first.")
        if top_k is None:
            top_k = self.top_k
        ngram_zscores_list = list(zip(self.get_ngram_zscores().index, self.get_ngram_zscores()['z-score']))
        top_k_class1 = list(reversed([x[0] for x in ngram_zscores_list[-top_k:]]))
        top_k_class2 = [x[0] for x in ngram_zscores_list[:top_k]]
        return top_k_class1, top_k_class2

    def get_ngrams_past_threshold(self, threshold: float = None) -> Tuple[List[str], List[str]]:
        """
        Returns the (ordered) ngrams that have absolute z-scores that exceed a specified threshold, for both classes
        :param threshold: if left as default None, FightingWords.threshold will be used.
        :return: two ordered lists of ngrams (with descending z-score):
                first list is for class 1, second list is for class 2
        """
        if self.ngram_zscores is None:
            raise ValueError("fit() must be run on a corpus first.")
        if threshold is None:
            threshold = self.threshold
        class1_ngrams = []
        class2_ngrams = []
        for ngram, zscore in self.ngram_zscores.items():
            if zscore > threshold:
                class1_ngrams.append(ngram)
            elif zscore < -threshold:
                class2_ngrams.append(ngram)
        return class1_ngrams[::-1], class2_ngrams

    def transform(self, corpus: Corpus) -> Corpus:
        """
        Annotates the corpus utterances with the lists of fighting words that the utterance contains
        The relevant fighting words to use are specified by FightingWords.top_k or FightingWords.threshold,
        with FightingWords.annot_method indicating which criterion to use.

        Lists are stored under metadata keys 'fighting_words_class1', 'fighting_words_class2'
        :param corpus: corpus to annotate
        :return: annotated corpus
        """
        class1_ngrams, class2_ngrams = self.get_top_k_ngrams() if self.annot_method == "top_k" \
                                else self.get_ngrams_past_threshold()

        for utt in corpus.iter_utterances(): # improve the efficiency of this; tricky because ngrams #TODO
            utt.meta['fighting_words_class1'] = [ngram for ngram in class1_ngrams if ngram in utt.text]
            utt.meta['fighting_words_class2'] = [ngram for ngram in class2_ngrams if ngram in utt.text]
        return corpus

    def get_zscore(self, ngram):
        """
        Get z-score of a given ngram
        :param ngram: ngram of interest
        :return: z-score value, None if zgram not in vocabulary
        """
        if self.ngram_zscores is None:
            raise ValueError("fit() must be run on a corpus first.")
        return self.ngram_zscores.get(ngram, None)

    def get_class(self, ngram):
        """
        Get the class that ngram more belongs to
        :param ngram: ngram of interest
        :return: "class1" if the ngram has non-negative z-score, "class2" if ngram has positive z-score, None if
                 ngram not in vocabulary
        """
        if self.ngram_zscores is None:
            raise ValueError("fit() must be run on a corpus first.")
        zscore = self.ngram_zscores.get(ngram, None)
        if zscore is None: return zscore
        if zscore >= 0:
            return "class1"
        else:
            return "class2"

    def summarize(self, corpus: Corpus, plot: bool = False):
        """
        Returns a DataFrame of ngram with zscores and classes, and optionally plots the fighting words distribution
        :param corpus: corpus to learn fighting words from if not already fitted
        :param plot: if True, generates a plot for the fighting words distribution
        :return: DataFrame of ngrams with zscores and classes, indexed by the ngrams (plot is optionally generated)
        """
        if self.ngram_zscores is None:
            self.fit(corpus)

        if plot:
            self.plot_fighting_words()
        return self.get_ngram_zscores()

    def plot_fighting_words(self, max_label_size=15):
        """
        Plots the distribution of fighting words
        Adapted from Xanda Schofield's https://gist.github.com/xandaschofield/3c4070b2f232b185ce6a09e47b4e7473

        Specifically, the weighted log-odds ratio is plotted against frequency of word within topic

        Only the most significant ngrams will have text labels. The most significant ngrams are specified by
        FightingWords.annot_method and (FightingWords.top_k or FightingWords.threshold)
        :param max_label_size: For the text labels, set the largest possible size for any text label
                                (the rest will be scaled accordingly)
        :return: None (plot is generated)
        """
        if self.ngram_zscores is None:
            raise ValueError("fit() must be run on a corpus first.")

        x_vals = self._count_matrix.sum(axis=0)
        y_vals = list(self.get_ngram_zscores()['z-score'])
        sizes = abs(np.array(y_vals))
        scale_factor = max_label_size / max(sizes)
        sizes *= scale_factor
        neg_color, pos_color, insig_color = ('orange', 'purple', 'grey')
        colors = []
        annots = []

        class1_sig_ngrams, class2_sig_ngrams = self.get_top_k_ngrams() if self.annot_method == "top_k" \
                                        else self.get_ngrams_past_threshold()
        class1_sig_ngrams = set(class1_sig_ngrams)
        class2_sig_ngrams = set(class2_sig_ngrams)

        terms = list(self.get_ngram_zscores().index)

        for i in range(len(terms)):
            if terms[i] in class1_sig_ngrams:
                colors.append(pos_color)
                annots.append(terms[i])
            elif terms[i] in class2_sig_ngrams:
                colors.append(neg_color)
                annots.append(terms[i])
            else:
                colors.append(insig_color)
                annots.append(None)

        fig, ax = plt.subplots()
        ax.scatter(x_vals, y_vals, c=colors, s=sizes, linewidth=0)
        for i, annot in enumerate(annots):
            if annot is not None:
                ax.annotate(annot, (x_vals[i], y_vals[i]), color=colors[i], size=sizes[i])
        ax.set_xscale('log')
        ax.set_title("Weighted log-odds ratio against Frequency of word within topic")
        plt.show()

    def get_model(self):
        return self.cv

    def set_model(self, cv):
        self.cv = cv

