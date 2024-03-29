import copy
import math
import random

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions.geometric as geom
from torch.nn import CrossEntropyLoss

from transformers import DistilBertPreTrainedModel, DistilBertModel

MASK_TOKEN = -100 # is this best way of initializing this?
PAD_TOKEN = 0
CLS_TOKEN = 101
SEP_TOKEN = 102
GAMMAS_INIT = [0.0]

class AuxMLMModel(DistilBertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.distilbert = DistilBertModel(config)
        self.qa_transformer_layer = copy.deepcopy(self.distilbert.transformer.layer[0])

        for layer in self.qa_transformer_layer.children(): # this may be missing a few of the more nested children layers...
            for sublayer in layer.children():              # but I think the important layers are all reset
                if hasattr(sublayer, 'reset_parameters'):
                    sublayer.reset_parameters()

        self.qa_outputs = nn.Linear(config.dim, config.num_labels)
        assert config.num_labels == 2
        self.dropout = nn.Dropout(config.qa_dropout)

        self.vocab_transform = nn.Linear(config.dim, config.dim)
        self.vocab_layer_norm = nn.LayerNorm(config.dim, eps=1e-12)
        self.vocab_projector = nn.Linear(config.dim, config.vocab_size)

        self.init_weights()

        self.mlm_probability = 0.15 # this is default for BERT and RoBERTa
        self.len_probability = 0.2 # span_length prob for geometric distribution
        self.max_spanlen = 2 # length of largest acceptable span

        self.mlm_loss_fct = nn.CrossEntropyLoss()

        self.vocab_size = None
        self.mask_token = MASK_TOKEN # maybe should come up with a better way of initializing this, in case we want to change it?
        self.gamma_idx = 0
        self.gammas = GAMMAS_INIT

    def set_mask_token(self, mask_token):
        self.mask_token = mask_token

    # rely on the training function to set the gammas as a function of training step
    def set_gammas(self, gammas):
        self.gammas = gammas
        self.gamma_idx = 0

    def get_gamma(self):
        return self.gammas[self.gamma_idx]

    # from MLM
    def get_output_embeddings(self):
        return self.vocab_projector

    # from MLM
    def set_output_embeddings(self, new_embeddings):
        self.vocab_projector = new_embeddings

    # add vocabulary size to model for MLM
    def add_vocab_size(self, vocab_size):
        # vocab should be a list of strings
        self.vocab_size = vocab_size
    
    # Roughly using SPANBert masking scheme https://arxiv.org/pdf/1907.10529.pdf
    def span_mask(self, inputs):
        if self.vocab_size is None:
            raise AttributeError('AuxMLMModel must have vocabulary size added via add_vocab_size() before training occurs')

        # from RoBERTa paper: https://github.com/huggingface/transformers/blob/master/src/transformers/data/data_collator.py#L356
        labels = inputs.clone()

        # detect tokens we should not mask
        special_tokens_mask = torch.zeros_like(inputs, device=inputs.device)
        special_tokens_mask[inputs == CLS_TOKEN] = 1 # which tokens can't be masked? [CLS], [SEP], [PAD]
        special_tokens_mask[inputs == SEP_TOKEN] = 1
        special_tokens_mask[inputs == PAD_TOKEN] = 1
        special_tokens_mask = special_tokens_mask.bool()

        #import pdb; pdb.set_trace()
        # Get the largest geometric sample of span lengths (batch_size, sent_len), clamped  
        ldist = geom.Geometric(torch.full(labels.shape, self.len_probability, device=inputs.device)).sample()
        ldist_trunc = torch.clamp(ldist, min=0.0, max=self.max_spanlen).float() + torch.ones_like(ldist).float() # geom produces [0, inf), we want 1-8
        sent_len = labels.shape[1] # lengths of input sentences (could pass this to the constructor)

        nmask = math.ceil(sent_len * self.mlm_probability)    # number of total modifications expected
        cumul = torch.cumsum(ldist_trunc, dim=1, dtype=float) # accumulate span lengths 
        lengths = torch.where(cumul < (nmask + 1 / self.len_probability) , ldist_trunc, torch.Tensor([0.]).to(inputs.device)) # only consider lengths up to ~nmask
        nspans = torch.unsqueeze(torch.count_nonzero(lengths, dim = 1), dim =1) # number of spans in each sentence
        lengths = lengths[:, :torch.max(nspans)]              # truncate length tensor to max span length


        # randomly (uniformly) generate anchoring indices for each span length, get ending indices
        start_idxs = torch.ceil(torch.rand_like(lengths, dtype=float) * sent_len).float()
        start_idxs = torch.where(lengths > 0., start_idxs, torch.Tensor([0.]).to(inputs.device)) # ignore indices with 0 length
        end_idxs = start_idxs + lengths # calculate stop indices for each span
        end_idxs = torch.clamp(end_idxs, min=0.0, max=sent_len) # clamp end indices 

        masked_spans =  torch.bernoulli(torch.full(lengths.shape, 0.8, device=inputs.device)).bool() # spans that will use [MASK]
        random_spans  = torch.bernoulli(torch.full(lengths.shape, 0.5, device=inputs.device)).bool() & ~masked_spans
        masked_spans = masked_spans.int(); random_spans = random_spans.int()
        masked_indices = torch.zeros_like(inputs, device=inputs.device, dtype=torch.int64) # initialize masks
        random_indices = torch.zeros_like(inputs, device=inputs.device, dtype=torch.int64) # initialize randomized indices
        increment = torch.ones_like(start_idxs).type(torch.LongTensor).to(inputs.device) # used to increment span indices
        current_idxs = start_idxs

        # fill in masking and randomized indices consecutively
        for i in range(self.max_spanlen):
            token_indices = torch.where(current_idxs < end_idxs, current_idxs, torch.Tensor([0.]).to(inputs.device)).type(torch.LongTensor).to(inputs.device)
            masked_indices.scatter_(1, token_indices * masked_spans, increment) # we scatter from a tensor of all 1s
            random_indices.scatter_(1, token_indices * random_spans, increment)
            current_idxs += increment
        
        # prevent masking protected tokens and convert to boolean tensors
        masked_indices.masked_fill_(special_tokens_mask, value=0)
        random_indices.masked_fill_(special_tokens_mask, value=0)
        masked_indices = masked_indices.bool()
        random_indices = random_indices.bool()

        # mask inputs and labels
        labels[~(masked_indices | random_indices)] = -100
        random_words = torch.randint(self.vocab_size, labels.shape, dtype=torch.long, device=inputs.device)
        inputs[masked_indices] = self.mask_token
        inputs[random_indices] = random_words[random_indices]

        return inputs, labels
        
   
    # Synchronous masking for MLM task
    def mlm_mask(self, inputs):
        # random.seed(0)
        # import pdb; pdb.set_trace()
        if self.vocab_size is None:
            raise AttributeError('AuxMLMModel must have vocabulary size added via add_vocab_size() before training occurs')

        # from RoBERTa paper: https://github.com/huggingface/transformers/blob/master/src/transformers/data/data_collator.py#L356
        labels = inputs.clone()

        # We sample a few tokens in each sequence for MLM training (15%)
        probability_matrix = torch.full(labels.shape, self.mlm_probability, device=inputs.device)

        special_tokens_mask = torch.zeros_like(inputs, device=inputs.device)
        special_tokens_mask[inputs == CLS_TOKEN] = 1 # which tokens can't be masked? [CLS], [SEP], [PAD]
        special_tokens_mask[inputs == SEP_TOKEN] = 1
        special_tokens_mask[inputs == PAD_TOKEN] = 1
        special_tokens_mask = special_tokens_mask.bool()

        probability_matrix.masked_fill_(special_tokens_mask, value=0.0)
        masked_indices = torch.bernoulli(probability_matrix).bool()
        labels[~masked_indices] = -100  # We only compute loss on masked tokens

        # 80% of the time, we replace masked input tokens with tokenizer.mask_token ([MASK])
        indices_replaced = torch.bernoulli(torch.full(labels.shape, 0.8, device=inputs.device)).bool() & masked_indices
        inputs[indices_replaced] = self.mask_token

        # 10% of the time, we replace masked input tokens with random word
        indices_random = torch.bernoulli(torch.full(labels.shape, 0.5, device=inputs.device)).bool() & masked_indices & ~indices_replaced
        random_words = torch.randint(self.vocab_size, labels.shape, dtype=torch.long, device=inputs.device)
        inputs[indices_random] = random_words[indices_random]

        # The rest of the time (10% of the time) we keep the masked input tokens unchanged
        return inputs, labels
        
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        head_mask=None,
        inputs_embeds=None,
        start_positions=None,
        end_positions=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        decay_gamma=False,
        mask_inputs=False,
    ):
        r"""
        start_positions (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
            Labels for position (index) of the start of the labelled span for computing the token classification loss.
            Positions are clamped to the length of the sequence (:obj:`sequence_length`). Position outside of the
            sequence are not taken into account for computing the loss.
        end_positions (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
            Labels for position (index) of the end of the labelled span for computing the token classification loss.
            Positions are clamped to the length of the sequence (:obj:`sequence_length`). Position outside of the
            sequence are not taken into account for computing the loss.
        """

        # import pdb; pdb.set_trace()
        if mask_inputs:
            input_ids, mlm_labels = self.span_mask(input_ids)
            #input_ids, mlm_labels = self.mlm_mask(input_ids) # mask inputs to both losses
        else:
            mlm_labels = input_ids # we don't care about MLM if we are not masking inputs

        # This is the result of DistilbertModel's forward method
        distilbert_output = self.distilbert(
            input_ids=input_ids,
            attention_mask=attention_mask,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict = return_dict,
        )

        hidden_states = distilbert_output[0]  # (bs, max_query_len, dim)

        hidden_states_qa = self.qa_transformer_layer(distilbert_output[0], attn_mask=attention_mask, output_attentions=output_attentions)[-1]
        
        # Compute logits from MLM
        prediction_logits = self.vocab_transform(hidden_states)  # (bs, max_query_length, dim)
        prediction_logits = F.gelu(prediction_logits)  # (bs, max_query_length, dim)
        prediction_logits = self.vocab_layer_norm(prediction_logits)  # (bs, max_query_length, dim)
        prediction_logits = self.vocab_projector(prediction_logits)  # (bs, max_query_length, vocab_size)

        # Compute logits from QA
        hidden_states_qa = self.dropout(hidden_states_qa)  # (bs, max_query_len, dim)
        logits = self.qa_outputs(hidden_states_qa)  # (bs, max_query_len, 2)
        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)  # (bs, max_query_len)
        end_logits = end_logits.squeeze(-1)  # (bs, max_query_len)

        # Compute Cross-Entropy Loss from QA
        qa_loss = None
        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = nn.CrossEntropyLoss(ignore_index=ignored_index)
            qa_start_loss = loss_fct(start_logits, start_positions)
            qa_end_loss = loss_fct(end_logits, end_positions)
            qa_loss = (qa_start_loss + qa_end_loss) / 2

        # Compute Cross-Entropy Loss from MLM, but only if we are actually masking inputs
        # note: CrossEntropyLoss automatically ignores all positions with value -100
        mlm_loss = 0
        if mask_inputs:
            mlm_loss = self.mlm_loss_fct(prediction_logits.view(-1, prediction_logits.size(-1)), mlm_labels.view(-1))

        # check that global_idx does not exceed size of gammas
        if self.gamma_idx > len(self.gammas) - 1:
            gamma_current = self.gammas[-1]
        else:
            gamma_current = self.gammas[self.gamma_idx]

        if decay_gamma:
            self.gamma_idx += 1

        # compute total loss        
        if qa_loss is None:
            total_loss = gamma_current * mlm_loss
        else:
            total_loss = qa_loss + gamma_current *  mlm_loss
            
        output = (start_logits, end_logits, prediction_logits) + distilbert_output[1:]

        return ((total_loss,) + output) if total_loss is not None else output


